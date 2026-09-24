# @covers AC-036, AC-037, AC-068, AC-070, AC-071, AC-072, AC-073, AC-075, AC-076, AC-078, AC-101, AC-102, AC-103
"""Phase orchestration shared by the CLI subcommands (scan -> ... -> synthesize)."""

from __future__ import annotations

import json
import math
import os
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dn import classify as classify_mod
from dn.apply import ApplyResult, actionable, apply_plan
from dn.distill import distill, facts_path
from dn.errors import EXIT_OK, EXIT_PARTIAL, ConfigError, CostLimitExceeded, DnError, worst
from dn.extract import EXTRACTABLE, extract_all, kind_by_ext, read_extracted, text_path
from dn.llm import (
    LLM,
    MAX_TOKENS,
    OUTPUT_ESTIMATE_RATIO,
    estimate_cost,
    estimate_tokens,
    require_api_key,
    resolve_mode,
)
from dn.plan import build_plan, load_plan, plan_summary, save_plan
from dn.scan import ScanResult, load_inventory, scan
from dn.schemas import InventoryEntry, LabelRecord, Plan
from dn.synthesize import synthesize
from dn.workspace import Workspace


@dataclass
class Options:
    dry_llm: bool = False
    no_images: bool = False
    follow_symlinks: bool = False
    include_hidden: bool = False
    full_hash: bool = False
    max_cost: float | None = None
    model: str | None = None
    synth_model: str | None = None
    concurrency: int | None = None
    lang: str | None = None
    copy: bool = False
    rename: bool | None = None
    dedupe: str | None = None
    interactive: bool = False
    distill_all: bool = False
    yes: bool = False
    json: bool = False
    fixtures_dir: Path | None = None
    llm_client: Any = None  # injected fake SDK client (tests)
    progress: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr)
    ask: Callable[[str], str] | None = None  # interactive prompt (tests inject)
    confirm: Callable[[str], bool] | None = None


@dataclass
class Report:
    data: dict[str, Any] = field(default_factory=dict)
    exit_code: int = EXIT_OK


class Pipeline:
    def __init__(self, ws: Workspace, opts: Options) -> None:
        _choice("--dedupe", opts.dedupe, ("move", "trash", "keep"))
        _choice("--lang", opts.lang, ("ja", "en", "auto"))
        self.ws = ws
        self.opts = opts
        cfg = ws.config
        if opts.model:
            cfg.model = opts.model
        if opts.synth_model:
            cfg.synth_model = opts.synth_model
        if opts.concurrency:
            cfg.concurrency = opts.concurrency
        if opts.lang:
            cfg.lang = opts.lang  # type: ignore[assignment]
        self.mode = resolve_mode(opts.dry_llm)
        self._llm: LLM | None = None
        self._synth: LLM | None = None

    # ---------------------------------------------------------- LLM clients
    def _make(self, model: str) -> LLM:
        fixtures = self.opts.fixtures_dir or (
            Path(os.environ["DN_LLM_FIXTURES"]) if os.environ.get("DN_LLM_FIXTURES") else None
        )
        return LLM(
            model=model,
            cache_dir=self.ws.llm_cache_dir,
            mode=self.mode,
            concurrency=self.ws.config.concurrency,
            fixtures_dir=fixtures,
            root=self.ws.root,
            root_aliases=self.ws.root_aliases,
            client=self.opts.llm_client,
            lang=self.ws.config.lang,
        )

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = self._make(self.ws.config.model)
        return self._llm

    @property
    def synth_llm(self) -> LLM:
        if self._synth is None:
            m = self.ws.config.effective_synth_model
            self._synth = self.llm if m == self.ws.config.model else self._make(m)
        return self._synth

    def check_llm_available(self) -> None:
        if self.opts.llm_client is None:
            require_api_key(self.mode)

    def usage(self) -> dict[str, Any]:
        u = self.llm.usage.as_dict() if self._llm else {}
        if self._synth is not None and self._synth is not self._llm:
            s = self._synth.usage.as_dict()
            u = {
                k: (u.get(k, 0) + v if isinstance(v, int | float) else {**u.get(k, {}), **v})
                for k, v in s.items()
            }
        return u

    def generated(self) -> int:
        n = self._llm.usage.generated if self._llm else 0
        if self._synth is not None and self._synth is not self._llm:
            n += self._synth.usage.generated
        return n

    # ---------------------------------------------------------- phases
    def scan(self) -> ScanResult:
        self.opts.progress("scan...")
        return scan(self.ws, self.opts.follow_symlinks, self.opts.include_hidden, self.opts.full_hash)

    async def extract(self, entries: list[InventoryEntry]) -> dict[str, Any]:
        self.opts.progress("extract...")
        stats = await extract_all(self.ws, entries, self.llm, want_images=not self.opts.no_images)
        return {
            "extracted": stats.extracted,
            "cached": stats.cached,
            "failed": stats.failed,
            "unsupported": stats.unsupported,
        }

    async def classify(self, entries: list[InventoryEntry]) -> dict[str, Any]:
        self.opts.progress("classify...")
        stats = await classify_mod.classify(self.ws, entries, self.llm)
        return {
            "classified": stats.classified,
            "cached": stats.cached,
            "failed": stats.failed,
            "low_confidence": stats.low_confidence,
        }

    def plan(self, entries: list[InventoryEntry]) -> Plan:
        ask = None
        if self.opts.interactive and not self.opts.json:
            prompt = self.opts.ask or (lambda q: input(q))

            # @assumption AS-014
            def ask(e: InventoryEntry, rec: LabelRecord) -> str | None:  # AS-014
                slugs = ", ".join(self.ws.taxonomy.slugs) if self.ws.taxonomy else ""
                ans = prompt(
                    f"{e.path}: '{rec.label.summary}' (confidence {rec.label.confidence:.2f}) "
                    f"-> category [{slugs}] (Enter keeps misc): "
                ).strip()
                return ans or None

        p = build_plan(
            self.ws, entries, copy=self.opts.copy, rename=self.opts.rename, dedupe=self.opts.dedupe, ask=ask
        )
        save_plan(self.ws, p)
        return p

    def confirm_apply(self, plan: Plan) -> None:
        if self.opts.yes:
            return
        if not sys.stdin.isatty() and self.opts.confirm is None:
            raise ConfigError("refusing to apply without confirmation: stdin is not a TTY; pass --yes")
        n = len(actionable(plan))
        warn = (
            " Files sent to the OS trash cannot be restored by `dn undo`."
            if any(pe.op == "trash" for pe in plan.entries)
            else ""
        )
        question = f"Apply {n} file operation(s)?{warn} [y/N] "
        ok = (
            self.opts.confirm(question)
            if self.opts.confirm
            else input(question).strip().lower() in ("y", "yes")
        )
        if not ok:
            raise DnError("aborted; nothing was moved")

    def ensure_confirmable(self) -> None:
        if not self.opts.yes and not sys.stdin.isatty() and self.opts.confirm is None:
            raise ConfigError("refusing to apply without confirmation: stdin is not a TTY; pass --yes")

    # ---------------------------------------------------------- cost
    def estimate(self, entries: list[InventoryEntry], phases: set[str]) -> dict[str, Any]:
        labels = classify_mod.load_labels(self.ws, classify_mod.taxonomy_key(self.ws.taxonomy))
        model, synth = self.ws.config.model, self.ws.config.effective_synth_model
        tin = tout = sin = sout = 0
        seen: set[str] = set()
        pending_classify = pending_distill = 0
        for e in entries:
            if not e.hash or e.skipped or e.hash in seen:
                continue
            seen.add(e.hash)
            tp = text_path(self.ws, e.hash)
            kind = kind_by_ext(e.ext)
            if tp.exists():
                meta, _ = read_extracted(tp)
                chars, lang = meta.chars, meta.lang
            elif kind == "image":
                chars, lang = 0, "en"
                if "extract" in phases and not self.opts.no_images:
                    tin += 1600 + 300
                    tout += int(MAX_TOKENS["vision"] * OUTPUT_ESTIMATE_RATIO)
            elif kind in EXTRACTABLE:
                chars, lang = min(e.size, 200_000), "en"
            else:
                continue
            if "classify" in phases and e.hash not in labels:
                pending_classify += 1
                tin += estimate_tokens(min(chars, 6000), lang) + 600
                tout += int(MAX_TOKENS["classify"] * OUTPUT_ESTIMATE_RATIO)
            if "distill" in phases and not facts_path(self.ws, e.hash).exists():
                pending_distill += 1
                n = max(1, math.ceil(chars / 8000))
                tin += estimate_tokens(chars, lang) + 500 * n
                tout += int(MAX_TOKENS["distill"] * OUTPUT_ESTIMATE_RATIO) * n
        if "synthesize" in phases and (pending_distill or pending_classify):
            sin += tout // 2 + 4000
            sout += int(MAX_TOKENS["synthesize"] * OUTPUT_ESTIMATE_RATIO) * 3
        cost = estimate_cost(model, tin, tout) + estimate_cost(synth, sin, sout)
        return {
            "input_tokens": tin + sin,
            "output_tokens": tout + sout,
            "cost_usd": round(cost, 4),
            "pending_classify": pending_classify,
            "pending_distill": pending_distill,
        }

    def check_cost(self, entries: list[InventoryEntry], phases: set[str]) -> dict[str, Any]:
        est = self.estimate(entries, phases)
        self.opts.progress(f"estimated LLM cost: ${est['cost_usd']:.4f}")
        if self.opts.max_cost is not None and est["cost_usd"] > self.opts.max_cost:
            raise CostLimitExceeded(
                f"estimated cost ${est['cost_usd']:.4f} exceeds --max-cost ${self.opts.max_cost:.4f}; nothing was sent"
            )
        return est

    # ---------------------------------------------------------- commands
    def finish(self, report: Report) -> Report:
        usage = self.usage()
        if usage:
            self.ws.write_usage(usage)
            report.data["llm"] = usage
        report.data["errors"] = self.ws.error_count
        if self.ws.warnings:
            report.data["warnings"] = self.ws.warnings
        if self.ws.error_count:
            report.exit_code = worst(report.exit_code, EXIT_PARTIAL)
        return report

    async def cmd_plan(self, propose: bool = False) -> Report:
        self.check_llm_available()
        sr = self.scan()
        est = self.check_cost(sr.entries, {"extract", "classify"})
        report = Report({"scan": sr.summary(), "estimate": est})
        report.data["extract"] = await self.extract(sr.entries)
        if propose or self.ws.taxonomy is None:
            # @assumption AS-012
            path = await classify_mod.propose_taxonomy(self.ws, sr.entries, self.llm)
            report.data["proposed_taxonomy"] = str(path)
            return self.finish(report)
        report.data["classify"] = await self.classify(sr.entries)
        p = self.plan(sr.entries)
        report.data["plan"] = {
            "summary": plan_summary(p),
            "entries": [pe.model_dump(by_alias=True) for pe in p.entries],
            "path": str(self.ws.plan_path),
        }
        return self.finish(report)

    def cmd_apply(self) -> Report:
        p = load_plan(self.ws)
        self.confirm_apply(p)
        res = apply_plan(self.ws, p)
        report = Report({"apply": _apply_dict(res)}, res.exit_code)
        return self.finish(report)

    async def cmd_distill(self) -> Report:
        self.check_llm_available()
        sr = self.scan()
        est = self.check_cost(sr.entries, {"extract", "classify", "distill", "synthesize"})
        report = Report({"scan": sr.summary(), "estimate": est})
        report.data["extract"] = await self.extract(sr.entries)
        report.data["classify"] = await self.classify(sr.entries)
        report.data.update(await self._knowledge(sr.entries))
        return self.finish(report)

    async def _knowledge(self, entries: list[InventoryEntry]) -> dict[str, Any]:
        self.opts.progress("distill...")
        ds = await distill(self.ws, entries, self.llm, self.opts.distill_all)
        self.opts.progress("synthesize...")
        syn = await synthesize(self.ws, entries, self.llm, self.synth_llm)
        return {
            "distill": {
                "distilled": ds.distilled,
                "cached": ds.cached,
                "skipped": ds.skipped,
                "failed": ds.failed,
            },
            "synthesize": {
                "files": syn.files,
                "topics": syn.topics,
                "failed_topics": syn.failed_topics,
                "split": syn.split,
                "index_tokens": syn.tokens,
                "path": str(self.ws.knowledge_dir),
            },
        }

    async def cmd_run(self, apply: bool = False) -> Report:
        if apply:
            self.ensure_confirmable()
        self.check_llm_available()
        sr = self.scan()
        est = self.check_cost(sr.entries, {"extract", "classify", "distill", "synthesize"})
        report = Report({"scan": sr.summary(), "estimate": est})
        report.data["extract"] = await self.extract(sr.entries)
        if self.ws.taxonomy is None:
            path = await classify_mod.propose_taxonomy(self.ws, sr.entries, self.llm)
            report.data["proposed_taxonomy"] = str(path)
            self.opts.progress(f"no taxonomy.yaml: proposal written to {path}; review it and rerun")
            return self.finish(report)
        report.data["classify"] = await self.classify(sr.entries)
        p = self.plan(sr.entries)
        report.data["plan"] = {"summary": plan_summary(p), "path": str(self.ws.plan_path)}
        entries = sr.entries
        moved = 0
        if apply and actionable(p):
            self.confirm_apply(p)
            res = apply_plan(self.ws, p)
            moved = res.moved + res.copied + res.trashed
            report.data["apply"] = _apply_dict(res)
            entries = self.scan().entries  # paths changed: knowledge sources point at organized paths
        report.data.update(await self._knowledge(entries))
        # @assumption AS-022
        changed = sr.changed_total > 0 or self.generated() > 0 or moved > 0
        report.data["changes"] = changed
        if not changed:
            self.opts.progress("no changes")
            report.data["message"] = "no changes"
        return self.finish(report)

    def cmd_status(self) -> Report:
        inv = load_inventory(self.ws)
        labels = classify_mod.load_labels(self.ws, classify_mod.taxonomy_key(self.ws.taxonomy))
        hashes = {e.hash for e in inv if e.hash and not e.skipped}
        extracted = sum(1 for h in hashes if text_path(self.ws, h).exists())
        labeled = sum(1 for h in hashes if h in labels)
        facts = sum(1 for h in hashes if facts_path(self.ws, h).exists())
        est = self.estimate(inv, {"extract", "classify", "distill", "synthesize"})
        llm_cache = len(list(self.ws.llm_cache_dir.glob("*.json"))) if self.ws.llm_cache_dir.exists() else 0
        data = {
            "files": len(inv),
            "unique": len(hashes),
            "skipped": sum(1 for e in inv if e.skipped),
            "cached": {
                "extracted": extracted,
                "labeled": labeled,
                "facts": facts,
                "llm_responses": llm_cache,
            },
            "unprocessed": {
                "extract": len(hashes) - extracted,
                "classify": est["pending_classify"],
                "distill": est["pending_distill"],
            },
            "estimate": est,
            "plan": self.ws.plan_path.exists(),
            "knowledge": self.ws.knowledge_dir.exists(),
        }
        return Report(data)

    def cmd_export(self, fmt: str, output: Path | None = None) -> Report:
        _choice("--format", fmt, ("md", "jsonl", "zip"))
        kdir = self.ws.knowledge_dir
        if not kdir.is_dir():
            raise DnError(f"no knowledge base at {kdir}; run `dn distill` first", exit_code=2)
        files = sorted(p for p in kdir.rglob("*") if p.is_file())
        ext = {"md": ".md", "jsonl": ".jsonl", "zip": ".zip"}[fmt]
        out = output or (self.ws.out / f"knowledge{ext}")
        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "zip":
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for p in files:
                    z.write(p, Path("_knowledge") / p.relative_to(kdir))
        elif fmt == "jsonl":
            with out.open("w", encoding="utf-8", newline="\n") as f:
                for p in files:
                    rel = p.relative_to(kdir).as_posix()
                    f.write(
                        json.dumps(
                            {"path": rel, "content": p.read_text(encoding="utf-8")}, ensure_ascii=False
                        )
                        + "\n"
                    )
        else:
            order = ["INDEX.md", "overview.md", "glossary.md"]
            ranked = sorted(
                files,
                key=lambda p: (
                    order.index(p.name) if p.parent == kdir and p.name in order else 99,
                    p.as_posix(),
                ),
            )
            parts = [
                f"<!-- file: {p.relative_to(kdir).as_posix()} -->\n\n{p.read_text(encoding='utf-8')}"
                for p in ranked
            ]
            out.write_text("\n\n".join(parts), encoding="utf-8", newline="\n")
        return Report({"export": str(out), "format": fmt, "files": len(files)})


def _apply_dict(res: ApplyResult) -> dict[str, Any]:
    return {
        "run_id": res.run_id,
        "moved": res.moved,
        "copied": res.copied,
        "trashed": res.trashed,
        "failed": res.failed,
    }


def _choice(name: str, value: str | None, choices: tuple[str, ...]) -> None:
    if value is not None and value not in choices:
        raise ConfigError(f"{name} must be one of {', '.join(choices)} (got {value!r})")
