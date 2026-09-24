# @covers AC-001, AC-002, AC-003, AC-004, AC-011, AC-012, AC-048, AC-077, AC-078, AC-079, AC-102, AC-103
"""``dn`` command line (typer). ``--json`` writes exactly one JSON document to stdout;
progress and logs go to stderr / ``.dn/logs/<run_id>.log``."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from dn import __version__
from dn.config import init_project
from dn.errors import EXIT_CONFIG, EXIT_OK, DnError
from dn.pipeline import Options, Pipeline, Report
from dn.workspace import Workspace

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Distill Nexus: organize files and distill domain knowledge.",
)

Target = Annotated[Path, typer.Argument(help="Target directory (default: current directory)")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Write a JSON result to stdout (progress to stderr)")]
OutOpt = Annotated[
    Path | None, typer.Option("--out", help="Organized output directory (default <target>/organized)")
]
DryOpt = Annotated[
    bool, typer.Option("--dry-llm", help="Do not call the API; return schema-valid dummy output")
]
DangerOpt = Annotated[
    bool,
    typer.Option("--i-know-what-i-am-doing", help="Allow the filesystem root or home directory as target"),
]
VerboseOpt = Annotated[
    int, typer.Option("-v", "--verbose", count=True, help="-v / -vv: more log output on stderr")
]
NoColorOpt = Annotated[bool, typer.Option("--no-color", help="Disable colored output")]
ModelOpt = Annotated[str | None, typer.Option("--model", help="Model for classify / extract / distill")]
SynthOpt = Annotated[
    str | None, typer.Option("--synth-model", help="Model for synthesize (overview / INDEX)")
]
ConcOpt = Annotated[
    int | None, typer.Option("--concurrency", min=1, max=32, help="Parallel LLM calls (default 6)")
]
CostOpt = Annotated[
    float | None,
    typer.Option("--max-cost", help="Stop before calling the API if the estimate exceeds this (USD)"),
]
LangOpt = Annotated[str | None, typer.Option("--lang", help="ja | en | auto")]
CopyOpt = Annotated[bool, typer.Option("--copy", help="Copy instead of move")]
RenameOpt = Annotated[
    bool | None, typer.Option("--rename/--no-rename", help="Rename to <slug>_<YYYY-MM-DD>.<ext>")
]
DedupeOpt = Annotated[str | None, typer.Option("--dedupe", help="move | trash | keep")]
InterOpt = Annotated[bool, typer.Option("--interactive", help="Confirm low-confidence files one by one")]
ImagesOpt = Annotated[bool, typer.Option("--no-images", help="Never send images to the API")]
LinksOpt = Annotated[bool, typer.Option("--follow-symlinks", help="Follow symbolic links while scanning")]
HiddenOpt = Annotated[bool, typer.Option("--include-hidden", help="Include hidden files")]
FullHashOpt = Annotated[
    bool, typer.Option("--full-hash", help="Hash whole files instead of the first 4MB + size")
]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")]
DistillAllOpt = Annotated[
    bool, typer.Option("--distill-all", help="Also distill misc files without domain knowledge")
]


class Ctx:
    def __init__(self, json_out: bool, verbose: int, no_color: bool) -> None:
        self.json = json_out
        self.err = Console(stderr=True, no_color=no_color, highlight=False, emoji=False, soft_wrap=True)
        self.out = Console(no_color=no_color, highlight=False, emoji=False, soft_wrap=True)
        level = logging.WARNING if verbose == 0 else (logging.INFO if verbose == 1 else logging.DEBUG)
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.handler = handler
        logging.getLogger("dn").addHandler(handler)

    def close(self) -> None:
        logging.getLogger("dn").removeHandler(self.handler)

    def progress(self, msg: str) -> None:
        self.err.print(f"[dim]{msg}[/dim]")


def _execute(
    ctx: Ctx,
    target: Path,
    body: Callable[[Workspace], Report | Coroutine[Any, Any, Report]],
    *,
    out: Path | None = None,
    danger: bool = False,
    render: Callable[[Ctx, Report], None] | None = None,
) -> None:
    code = EXIT_OK
    ws: Workspace | None = None
    try:
        ws = Workspace.open(target, out=out, allow_dangerous=danger)
        log_path = ws.setup_logging()
        res = body(ws)
        report = asyncio.run(res) if asyncio.iscoroutine(res) else res
        assert isinstance(report, Report)
        report.data.setdefault("run_id", ws.run_id)
        report.data.setdefault("log", str(log_path))
        code = report.exit_code
        report.data["exit_code"] = code
        if ctx.json:
            sys.stdout.write(json.dumps(report.data, ensure_ascii=False, default=str) + "\n")
        elif render:
            render(ctx, report)
        else:
            ctx.out.print_json(json.dumps(report.data, ensure_ascii=False, default=str))
    except DnError as e:
        code = e.exit_code
        if ws is not None:
            logging.getLogger("dn").error("%s", e)
        if ctx.json:
            sys.stdout.write(json.dumps({"error": str(e), "exit_code": code}, ensure_ascii=False) + "\n")
        ctx.err.print(f"[red]error:[/red] {e}", markup=True, highlight=False)
    except KeyboardInterrupt:
        code = 130
        ctx.err.print("interrupted")
    finally:
        if ws is not None:
            ws.close_logging()
        ctx.close()
    raise typer.Exit(code)


def _options(ctx: Ctx, **kw: Any) -> Options:
    return Options(json=ctx.json, progress=ctx.progress, **kw)


# ------------------------------------------------------------------ renderers


def _render_scan(ctx: Ctx, r: Report) -> None:
    s = r.data["scan"]
    ctx.out.print(f"files: {s['files']}  (hashed now: {s['hash_computed']}, skipped: {len(s['skipped'])})")
    t = Table("type", "count")
    for k, v in s["by_type"].items():
        t.add_row(k, str(v))
    ctx.out.print(t)
    if s["duplicates"]:
        ctx.out.print("duplicate candidates:")
        for paths in s["duplicates"].values():
            ctx.out.print("  " + "  ==  ".join(paths))


def _render_plan(ctx: Ctx, r: Report) -> None:
    if "proposed_taxonomy" in r.data:
        ctx.out.print(f"taxonomy proposal written to {r.data['proposed_taxonomy']}")
        ctx.out.print("review it, save it as taxonomy.yaml, then run `dn plan` again")
        return
    plan = r.data["plan"]
    t = Table("from", "to", "category", "conf", "op", title="plan (dry run - nothing was moved)")
    for pe in plan["entries"]:
        if pe["op"] == "noop":
            continue
        t.add_row(pe["from"], pe["to"] or "-", pe["category"], f"{pe['confidence']:.2f}", pe["op"])
    ctx.out.print(t)
    ctx.out.print(f"summary: {plan['summary']}  ->  run `dn apply` to execute")


def _render_generic(ctx: Ctx, r: Report) -> None:
    if r.data.get("message"):
        ctx.out.print(r.data["message"])
    ctx.out.print_json(
        json.dumps({k: v for k, v in r.data.items() if k not in ("log",)}, ensure_ascii=False, default=str)
    )


# ------------------------------------------------------------------ commands


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit(0)


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version")
    ] = False,
) -> None:
    """Distill Nexus (dn)."""


@app.command()
def init(target: Target = Path("."), json_out: JsonOpt = False, no_color: NoColorOpt = False) -> None:
    """Create .dn/config.yaml and taxonomy.yaml templates (existing files are kept)."""
    ctx = Ctx(json_out, 0, no_color)
    code = EXIT_OK
    try:
        root = target.expanduser().resolve()
        if not root.is_dir():
            raise DnError(f"not a directory: {root}", exit_code=EXIT_CONFIG)
        created = init_project(root)
        if json_out:
            sys.stdout.write(json.dumps({"created": [str(p) for p in created]}) + "\n")
        else:
            for p in created:
                ctx.out.print(f"created {p}")
            if not created:
                ctx.out.print("nothing to do (files already exist)")
    except DnError as e:
        code = e.exit_code
        ctx.err.print(f"error: {e}")
    finally:
        ctx.close()
    raise typer.Exit(code)


@app.command("scan")
def scan_cmd(
    target: Target = Path("."),
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    verbose: VerboseOpt = 0,
    no_color: NoColorOpt = False,
    follow_symlinks: LinksOpt = False,
    include_hidden: HiddenOpt = False,
    full_hash: FullHashOpt = False,
) -> None:
    """Walk and hash files; show counts, types and duplicate candidates."""
    ctx = Ctx(json_out, verbose, no_color)
    opts = _options(ctx, follow_symlinks=follow_symlinks, include_hidden=include_hidden, full_hash=full_hash)

    def body(ws: Workspace) -> Report:
        return Report({"scan": Pipeline(ws, opts).scan().summary()})

    _execute(ctx, target, body, out=out, danger=danger, render=_render_scan)


@app.command()
def plan(
    target: Target = Path("."),
    propose_taxonomy: Annotated[bool, typer.Option("--propose-taxonomy")] = False,
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    verbose: VerboseOpt = 0,
    no_color: NoColorOpt = False,
    dry_llm: DryOpt = False,
    model: ModelOpt = None,
    concurrency: ConcOpt = None,
    max_cost: CostOpt = None,
    lang: LangOpt = None,
    copy: CopyOpt = False,
    rename: RenameOpt = None,
    dedupe: DedupeOpt = None,
    interactive: InterOpt = False,
    no_images: ImagesOpt = False,
    follow_symlinks: LinksOpt = False,
    include_hidden: HiddenOpt = False,
    full_hash: FullHashOpt = False,
) -> None:
    """Extract + classify + build the move plan (dry run)."""
    ctx = Ctx(json_out, verbose, no_color)
    opts = _options(
        ctx,
        dry_llm=dry_llm,
        model=model,
        concurrency=concurrency,
        max_cost=max_cost,
        lang=lang,
        copy=copy,
        rename=rename,
        dedupe=dedupe,
        interactive=interactive,
        no_images=no_images,
        follow_symlinks=follow_symlinks,
        include_hidden=include_hidden,
        full_hash=full_hash,
    )
    _execute(
        ctx,
        target,
        lambda ws: Pipeline(ws, opts).cmd_plan(propose_taxonomy),
        out=out,
        danger=danger,
        render=_render_plan,
    )


@app.command()
def apply(
    target: Target = Path("."),
    yes: YesOpt = False,
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    verbose: VerboseOpt = 0,
    no_color: NoColorOpt = False,
) -> None:
    """Execute plan.json (asks for confirmation unless --yes)."""
    ctx = Ctx(json_out, verbose, no_color)
    opts = _options(ctx, yes=yes)
    _execute(
        ctx, target, lambda ws: Pipeline(ws, opts).cmd_apply(), out=out, danger=danger, render=_render_generic
    )


@app.command("distill")
def distill_cmd(
    target: Target = Path("."),
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    verbose: VerboseOpt = 0,
    no_color: NoColorOpt = False,
    dry_llm: DryOpt = False,
    model: ModelOpt = None,
    synth_model: SynthOpt = None,
    concurrency: ConcOpt = None,
    max_cost: CostOpt = None,
    lang: LangOpt = None,
    no_images: ImagesOpt = False,
    distill_all: DistillAllOpt = False,
) -> None:
    """Extract knowledge and (re)generate _knowledge/."""
    ctx = Ctx(json_out, verbose, no_color)
    opts = _options(
        ctx,
        dry_llm=dry_llm,
        model=model,
        synth_model=synth_model,
        concurrency=concurrency,
        max_cost=max_cost,
        lang=lang,
        no_images=no_images,
        distill_all=distill_all,
    )
    _execute(
        ctx,
        target,
        lambda ws: Pipeline(ws, opts).cmd_distill(),
        out=out,
        danger=danger,
        render=_render_generic,
    )


@app.command()
def run(
    target: Target = Path("."),
    do_apply: Annotated[bool, typer.Option("--apply", help="Also execute the plan")] = False,
    yes: YesOpt = False,
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    verbose: VerboseOpt = 0,
    no_color: NoColorOpt = False,
    dry_llm: DryOpt = False,
    model: ModelOpt = None,
    synth_model: SynthOpt = None,
    concurrency: ConcOpt = None,
    max_cost: CostOpt = None,
    lang: LangOpt = None,
    copy: CopyOpt = False,
    rename: RenameOpt = None,
    dedupe: DedupeOpt = None,
    interactive: InterOpt = False,
    no_images: ImagesOpt = False,
    distill_all: DistillAllOpt = False,
    follow_symlinks: LinksOpt = False,
    include_hidden: HiddenOpt = False,
    full_hash: FullHashOpt = False,
) -> None:
    """scan -> plan -> (apply) -> distill -> synthesize."""
    ctx = Ctx(json_out, verbose, no_color)
    opts = _options(
        ctx,
        dry_llm=dry_llm,
        model=model,
        synth_model=synth_model,
        concurrency=concurrency,
        max_cost=max_cost,
        lang=lang,
        copy=copy,
        rename=rename,
        dedupe=dedupe,
        interactive=interactive,
        no_images=no_images,
        distill_all=distill_all,
        yes=yes,
        follow_symlinks=follow_symlinks,
        include_hidden=include_hidden,
        full_hash=full_hash,
    )
    _execute(
        ctx,
        target,
        lambda ws: Pipeline(ws, opts).cmd_run(do_apply),
        out=out,
        danger=danger,
        render=_render_generic,
    )


@app.command("undo")
def undo_cmd(
    target: Target = Path("."),
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    verbose: VerboseOpt = 0,
    no_color: NoColorOpt = False,
) -> None:
    """Roll back the latest (or the given) apply run."""
    from dn.apply import undo

    ctx = Ctx(json_out, verbose, no_color)

    def body(ws: Workspace) -> Report:
        res = undo(ws, run_id)
        for s in res.skipped:
            ctx.err.print(f"warning: skipped {s}")
        if res.trashed:
            ctx.err.print("warning: these files were sent to the OS trash and must be restored manually:")
            for p in res.trashed:
                ctx.err.print(f"  {p}")
        data: dict[str, Any] = {
            "undo": {
                "run_id": res.run_id,
                "restored": res.restored,
                "removed_copies": res.removed_copies,
                "skipped": res.skipped,
                "trashed": res.trashed,
            }
        }
        if res.run_id is None:
            data["message"] = "nothing to undo"
        return Report(data, res.exit_code)

    _execute(ctx, target, body, out=out, danger=danger, render=_render_generic)


@app.command()
def status(
    target: Target = Path("."),
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    verbose: VerboseOpt = 0,
    no_color: NoColorOpt = False,
    model: ModelOpt = None,
    synth_model: SynthOpt = None,
) -> None:
    """Cache state, unprocessed counts and estimated cost."""
    ctx = Ctx(json_out, verbose, no_color)
    opts = _options(ctx, model=model, synth_model=synth_model)
    _execute(
        ctx,
        target,
        lambda ws: Pipeline(ws, opts).cmd_status(),
        out=out,
        danger=danger,
        render=_render_generic,
    )


@app.command()
def export(
    target: Target = Path("."),
    fmt: Annotated[str, typer.Option("--format", help="md | jsonl | zip")] = "md",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    json_out: JsonOpt = False,
    out: OutOpt = None,
    danger: DangerOpt = False,
    no_color: NoColorOpt = False,
) -> None:
    """Bundle _knowledge/ into a single file."""
    ctx = Ctx(json_out, 0, no_color)
    _execute(
        ctx,
        target,
        lambda ws: Pipeline(ws, Options(json=json_out)).cmd_export(fmt, output),
        out=out,
        danger=danger,
        render=_render_generic,
    )


def main() -> None:
    """Console entry point; usage errors exit with 2, DnError with its own code."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
