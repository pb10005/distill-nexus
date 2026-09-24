"""FEAT-003: LLM call layer."""

from __future__ import annotations

import asyncio
import getpass
import json
from pathlib import Path

import pytest
from conftest import (
    FakeClient,
    api_error,
    connection_error,
    dn_json,
    label,
    make_llm,
    open_ws,
    smart_handler,
    tool_of,
    user_text,
    with_taxonomy,
    write,
)

from dn import llm as llm_mod
from dn.classify import classify
from dn.llm import LLMCallError, LLMReplayMissing, detect_lang, estimate_tokens
from dn.pipeline import Options, Pipeline
from dn.scan import scan
from dn.schemas import Distilled, Label, MarkdownDoc, SamePairs, Topics, VisionResult


def _call(llm, content: str = "hello", dry=None):  # type: ignore[no-untyped-def]
    return asyncio.run(
        llm.structured(
            "classify",
            "classify",
            Label,
            content,
            dry=dry or (lambda: Label(category="misc", confidence=0.1, title="t", summary="s", tags=[])),
            schema_overrides={"categories": ["specs"]},
        )
    )


def test_schema_retry_with_error(tmp_path: Path):
    """AC-028: an invalid first output triggers one regeneration that carries the validation error."""
    outputs = [label("specs", 2), label("specs", 0.8)]
    client = FakeClient(lambda req, n: outputs[n - 1])
    llm = make_llm(tmp_path, client)
    result = _call(llm)
    assert result.confidence == 0.8
    assert len(client.requests) == 2
    second = user_text(client.requests[1])
    assert "failed validation" in second and "confidence" in second


def test_schema_fails_twice_recorded(target: Path):
    """AC-029: two invalid outputs -> the file is recorded in errors.jsonl and no third call is made."""
    with_taxonomy(target)
    write(target, "a.md", "# spec\n")
    ws = open_ws(target)
    entries = scan(ws).entries
    from dn.extract import extract_all

    asyncio.run(extract_all(ws, entries, make_llm(target / ".dn", mode="dry")))
    client = FakeClient(lambda req, n: label("specs", 7))
    stats = asyncio.run(classify(ws, entries, make_llm(target / ".dn", client)))
    assert len(client.requests) == 2
    assert stats.failed == 1
    err = json.loads(ws.errors_path.read_text(encoding="utf-8").splitlines()[0])
    assert err["path"] == "a.md" and err["phase"] == "classify"


@pytest.mark.parametrize(
    "make_err", [lambda: api_error(429), lambda: api_error(529), lambda: api_error(500), connection_error]
)
def test_retry_backoff(tmp_path: Path, make_err):
    """AC-030: 429/529/5xx/connection errors retry 5 times with exponential backoff, then fail."""
    client = FakeClient(lambda req, n: make_err())
    delays: list[float] = []

    async def record(d: float) -> None:
        delays.append(d)

    llm = make_llm(tmp_path, client, sleep=record)
    with pytest.raises(LLMCallError):
        _call(llm)
    assert len(client.requests) == 6
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_no_retry_on_400(tmp_path: Path):
    """AC-031: a 400 is not retried (one API call)."""
    client = FakeClient(lambda req, n: api_error(400))
    llm = make_llm(tmp_path, client)
    with pytest.raises(LLMCallError):
        _call(llm)
    assert len(client.requests) == 1


def test_cache_and_prompt_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """AC-032: an identical request is served from cache; a changed prompt calls the API again."""
    client = FakeClient(lambda req, n: label("specs", 0.9))
    llm = make_llm(tmp_path, client)
    first = _call(llm, "same input")
    again = _call(make_llm(tmp_path, client), "same input")
    assert len(client.requests) == 1 and first == again
    original = llm_mod.load_prompt
    monkeypatch.setattr(llm_mod, "load_prompt", lambda name: original(name) + "\nExtra rule.")
    changed = _call(make_llm(tmp_path, client), "same input")
    assert len(client.requests) == 2
    assert changed.category == "specs"


class _Explode:
    class messages:  # noqa: N801
        @staticmethod
        async def create(**_: object) -> None:
            raise AssertionError("the API must not be called in dry mode")


def test_dry_llm_all_schemas(target: Path):
    """AC-033: --dry-llm returns schema-valid dummies for classify, distill and synthesize without the API."""
    with_taxonomy(target)
    write(
        target,
        "specs/api.md",
        "# API Gateway\n\nThe gateway routes requests.\n\n## Limits\n\nThe limit is 100 rps.\n",
    )
    write(target, "nda.txt", "contracts nda agreement between A and B")
    ws = open_ws(target)
    p = Pipeline(ws, Options(dry_llm=True, llm_client=_Explode(), progress=lambda m: None))
    report = asyncio.run(p.cmd_run())
    assert report.exit_code == 0
    assert report.data["llm"]["api_calls"] == 0 and report.data["llm"]["generated"] > 0
    assert (ws.knowledge_dir / "INDEX.md").exists()
    assert report.data["classify"]["classified"] == 2
    assert report.data["distill"]["distilled"] >= 1
    assert report.data["synthesize"]["topics"] >= 1
    llm = make_llm(target, _Explode(), mode="dry")
    for model, dummy in [
        (Label, lambda: Label(category="misc", confidence=0.1, title="t", summary="s", tags=[])),
        (Distilled, lambda: Distilled()),
        (SamePairs, lambda: SamePairs()),
        (Topics, lambda: Topics()),
        (MarkdownDoc, lambda: MarkdownDoc(markdown="x")),
        (VisionResult, lambda: VisionResult(transcript="", description="d")),
    ]:
        out = asyncio.run(llm.structured("distill", "distill", model, f"{model.__name__}", dry=dummy))  # type: ignore[arg-type]
        assert isinstance(out, model)
        model.model_validate(out.model_dump(by_alias=True))


def test_replay(tmp_path: Path):
    """AC-034: DN_LLM_REPLAY returns recordings; a missing recording raises an error naming its key."""
    fixtures = tmp_path / "fixtures"
    recorder = make_llm(
        tmp_path / "a",
        FakeClient(lambda req, n: label("specs", 0.7, title="Recorded")),
        mode="record",
        fixtures_dir=fixtures,
    )
    _call(recorder, "recorded input")
    assert len(list(fixtures.glob("*.json"))) == 1
    replay = make_llm(tmp_path / "b", None, mode="replay", fixtures_dir=fixtures)
    assert _call(replay, "recorded input").title == "Recorded"
    with pytest.raises(LLMReplayMissing) as ei:
        _call(replay, "never recorded")
    req = replay.build_request(
        "classify",
        llm_mod.load_prompt("classify"),
        "submit_classify",
        Label.tool_schema(categories=["specs"]),
        "never recorded",
    )
    assert replay.cache_key(req) in str(ei.value)


def test_token_estimate():
    """AC-035: 10,000 English chars -> 4,000 tokens; 10,000 Japanese chars -> 6,667 tokens."""
    en = ("gateway " * 1250)[:10000]
    ja = ("日本語の文章です。" * 1200)[:10000]
    assert estimate_tokens(en, detect_lang(en)) == 4000
    assert abs(estimate_tokens(ja, detect_lang(ja)) - 6667) <= 1


@pytest.mark.parametrize("cmd", ["plan", "run"])
def test_max_cost_stops_before_llm(target: Path, cmd: str, tmp_path: Path):
    """AC-036: an estimate above --max-cost stops with exit 4 before any LLM call."""
    with_taxonomy(target)
    write(target, "a.md", "# spec\n" + "text " * 2000)
    empty = tmp_path / "no-recordings"
    empty.mkdir()
    code, out = dn_json(
        cmd,
        str(target),
        "--max-cost",
        "0.000001",
        env={"DN_LLM_REPLAY": "1", "DN_LLM_FIXTURES": str(empty), "ANTHROPIC_API_KEY": ""},
    )
    assert code == 4, out
    assert "max-cost" in out["error"]
    cache = target / ".dn" / "cache"
    assert not (cache / "llm").exists()
    assert not (cache / "text").exists()


def test_usage_log(target: Path):
    """AC-037: .dn/logs/<run_id>.json records input/output/cache-read tokens and cost in USD."""
    with_taxonomy(target)
    write(target, "a.md", "# API Gateway\n\nRoutes requests.\n")
    ws = open_ws(target)
    client = FakeClient(smart_handler())
    report = asyncio.run(Pipeline(ws, Options(llm_client=client, progress=lambda m: None)).cmd_run())
    data = json.loads((ws.logs_dir / f"{ws.run_id}.json").read_text(encoding="utf-8"))
    assert data["input_tokens"] == 100 * len(client.requests)
    assert data["output_tokens"] == 20 * len(client.requests)
    assert "cache_read_input_tokens" in data
    assert data["cost_usd"] > 0
    assert report.data["llm"]["api_calls"] == len(client.requests)


def test_privacy_scrub(tmp_path: Path):
    """AC-038: request bodies never contain the target's absolute path or the OS user name."""
    home_like = tmp_path / "Users" / "someone" / "docs"
    user = getpass.getuser()
    write(home_like, "notes.md", f"# Notes\n\nSaved at {home_like}/notes.md by {user}.\n")
    with_taxonomy(home_like)
    ws = open_ws(home_like)
    client = FakeClient(smart_handler())
    asyncio.run(Pipeline(ws, Options(llm_client=client, progress=lambda m: None)).cmd_run())
    assert client.requests
    import re

    for req in client.requests:
        body = json.dumps(req, ensure_ascii=False)
        assert str(home_like) not in body and home_like.as_posix() not in body
        assert not re.search(rf"(?<![\w]){re.escape(user)}(?![\w])", body), user


def test_request_parameters(target: Path):
    """AC-039: temperature 0, per-phase max_tokens, cache_control on system, forced tool_choice."""
    with_taxonomy(target)
    write(target, "a.md", "# API Gateway\n\nRoutes requests.\n")
    client = FakeClient(smart_handler())
    ws = open_ws(target)
    asyncio.run(Pipeline(ws, Options(llm_client=client, progress=lambda m: None)).cmd_run())
    by_tool: dict[str, dict[str, object]] = {tool_of(r): r for r in client.requests}
    assert by_tool["submit_classify"]["max_tokens"] == 1000
    assert by_tool["submit_distill"]["max_tokens"] == 4000
    assert by_tool["submit_topic_doc"]["max_tokens"] == 8000
    for tool, req in by_tool.items():
        assert req["model"] == "claude-sonnet-4-6"
        assert req["temperature"] == 0, tool
        assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}  # type: ignore[index]
        assert req["tool_choice"] == {"type": "tool", "name": tool}
