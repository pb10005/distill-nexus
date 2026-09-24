# @covers AC-011, AC-024, AC-028, AC-029, AC-030, AC-031, AC-032, AC-033, AC-034, AC-035, AC-036, AC-037, AC-038, AC-039
"""The single entry point for every Anthropic call.

Guarantees, in order of application:
  1. cache  - key = blake2b(model + system prompt + tool schema + request content);
              an identical request never reaches the API twice.
  2. modes  - ``dry`` returns schema-valid dummies, ``replay`` reads recordings,
              ``live`` (optionally ``record``) calls the API.
  3. schema - output is forced through a tool ``input_schema`` and re-validated with
              pydantic; one regeneration with the error attached, then give up.
  4. retry  - 429 / 529 / 5xx / connection errors back off exponentially (5 retries);
              anything else (400 ...) fails immediately.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import ValidationError

from dn.errors import ConfigError, DnError
from dn.schemas import LLMModel

log = logging.getLogger("dn.llm")

T = TypeVar("T", bound=LLMModel)
Mode = Literal["live", "dry", "replay", "record"]

PROMPTS_DIR = Path(__file__).parent / "prompts"
# @assumption AS-010
DEFAULT_FIXTURES = Path("tests") / "fixtures" / "llm"

# §6 共通ルール: max_tokens per phase
MAX_TOKENS = {"classify": 1000, "propose": 4000, "vision": 4000, "distill": 4000, "synthesize": 8000}
MAX_RETRIES = 5
RETRY_BASE_SECONDS = 1.0

# @assumption AS-011 (USD per million tokens: input, output)
PRICES = {"opus": (15.0, 75.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
OUTPUT_ESTIMATE_RATIO = 0.3


class LLMSchemaError(DnError):
    """Output failed validation twice."""


class LLMReplayMissing(DnError):
    """DN_LLM_REPLAY=1 and no recording exists for this request."""


class LLMCallError(DnError):
    """Non-retryable API failure, or retries exhausted."""


# ------------------------------------------------------------------ prompts & tokens


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


_JA = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")


# @assumption AS-007
def detect_lang(text: str) -> str:
    """AS-007: >=20% kana/kanji among letters -> ja; latin letters -> en; else und."""
    sample = text[:20000]
    ja = len(_JA.findall(sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    letters = ja + latin
    if letters == 0:
        return "und"
    return "ja" if ja / letters >= 0.2 else ("en" if latin else "und")


def estimate_tokens(text_or_chars: str | int, lang: str = "en") -> int:
    """§9: characters / 2.5 (Japanese / 1.5)."""
    chars = text_or_chars if isinstance(text_or_chars, int) else len(text_or_chars)
    return round(chars / (1.5 if lang == "ja" else 2.5))


def price_for(model: str) -> tuple[float, float]:
    for key, price in PRICES.items():
        if key in model:
            return price
    return PRICES["sonnet"]


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price_for(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


# @assumption AS-023
_NO_TEMPERATURE = re.compile(r"claude-(opus-4-[7-9]|opus-[5-9]|sonnet-[5-9]|fable|mythos)")
_NO_FORCED_TOOL = re.compile(r"claude-(opus-5-5|fable-5-1|mythos-5-1)")


def supports_temperature(model: str) -> bool:
    return not _NO_TEMPERATURE.search(model)


def supports_forced_tool(model: str) -> bool:
    return not _NO_FORCED_TOOL.search(model)


# ------------------------------------------------------------------ privacy


def scrub(text: str, root: Path | None) -> str:
    """Remove the absolute target path, the home path and the OS user name (§9 privacy)."""
    if root is not None:
        for variant in {str(root), root.as_posix()}:
            text = text.replace(variant, "<target>")
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~").replace(Path.home().as_posix(), "~")
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no user database
        user = ""
    if len(user) >= 2:
        text = re.sub(rf"(?<![\w]){re.escape(user)}(?![\w])", "<user>", text)
    return text


def _scrub_content(content: Any, root: Path | None) -> Any:
    if isinstance(content, str):
        return scrub(content, root)
    if isinstance(content, list):
        return [_scrub_content(c, root) for c in content]
    if isinstance(content, dict):
        if content.get("type") == "image":
            return content
        return {k: _scrub_content(v, root) for k, v in content.items()}
    return content


# ------------------------------------------------------------------ usage


@dataclass
class Usage:
    calls: int = 0  # real API requests (including retries)
    generated: int = 0  # cache misses served by the active backend (api / dry / replay)
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, model: str, usage: Any) -> None:
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cc = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_input_tokens += cr
        self.cache_creation_input_tokens += cc
        pin, pout = price_for(model)
        self.cost_usd += (inp * pin + out * pout + cr * pin * 0.1 + cc * pin * 1.25) / 1_000_000
        m = self.by_model.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
        m["input_tokens"] += inp
        m["output_tokens"] += out

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_calls": self.calls,
            "generated": self.generated,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "by_model": self.by_model,
        }


# ------------------------------------------------------------------ client


def resolve_mode(dry: bool) -> Mode:
    if dry:
        return "dry"
    if os.environ.get("DN_LLM_REPLAY") == "1":
        return "replay"
    if os.environ.get("DN_LLM_RECORD") == "1":
        return "record"
    return "live"


def require_api_key(mode: Mode) -> None:
    if mode in ("live", "record") and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Export it, or use --dry-llm (dummy output) "
            "or DN_LLM_REPLAY=1 (recorded responses)."
        )


class LLM:
    def __init__(
        self,
        model: str,
        cache_dir: Path,
        mode: Mode = "live",
        concurrency: int = 6,
        fixtures_dir: Path | None = None,
        root: Path | None = None,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lang: str = "auto",
    ) -> None:
        self.model = model
        self.cache_dir = cache_dir
        self.mode: Mode = mode
        self.fixtures_dir = fixtures_dir or DEFAULT_FIXTURES
        self.root = root
        self.usage = Usage()
        self.lang = lang
        self._client = client
        self._sleep = sleep
        self._concurrency = concurrency
        self._sem: asyncio.Semaphore | None = None

    # -- infrastructure
    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._concurrency)
        return self._sem

    def client(self) -> Any:
        if self._client is None:
            require_api_key(self.mode)
            from anthropic import AsyncAnthropic

            # @assumption AS-009 - dn owns retries, so the SDK's are disabled
            self._client = AsyncAnthropic(max_retries=0)
        return self._client

    @staticmethod
    def cache_key(request: dict[str, Any]) -> str:
        blob = json.dumps(request, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.blake2b(blob.encode("utf-8"), digest_size=20).hexdigest()

    def build_request(
        self,
        phase: str,
        system: str,
        tool_name: str,
        schema: dict[str, Any],
        content: str | list[dict[str, Any]],
        model: str | None = None,
        shared_context: str | None = None,
    ) -> dict[str, Any]:
        model = model or self.model
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if shared_context:
            system_blocks.append({"type": "text", "text": shared_context})
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        tool = {"name": tool_name, "description": f"Return the {phase} result.", "input_schema": schema}
        req: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS.get(phase, 4000),
            "system": system_blocks,
            "tools": [tool],
            "messages": [{"role": "user", "content": _scrub_content(content, self.root)}],
        }
        if supports_temperature(model):
            req["temperature"] = 0
        if supports_forced_tool(model):
            req["tool_choice"] = {"type": "tool", "name": tool_name}
        else:
            req["tool_choice"] = {"type": "auto"}
            system_blocks.insert(
                0, {"type": "text", "text": f"Always answer by calling the `{tool_name}` tool."}
            )
        return req

    # -- the call
    async def structured(
        self,
        phase: str,
        prompt: str,
        output: type[T],
        content: str | list[dict[str, Any]],
        *,
        dry: Callable[[], T],
        schema_overrides: dict[str, Any] | None = None,
        shared_context: str | None = None,
        model: str | None = None,
        validate: Callable[[T], None] | None = None,
    ) -> T:
        system = load_prompt(prompt)
        if self.lang in ("ja", "en"):
            system += f"\n\nOutput language: {'Japanese' if self.lang == 'ja' else 'English'}."
        schema = output.tool_schema(**(schema_overrides or {}))
        req = self.build_request(phase, system, f"submit_{prompt}", schema, content, model, shared_context)

        error: str | None = None
        for attempt in range(2):
            if error is not None:
                req = json.loads(json.dumps(req))
                req["messages"][0]["content"] = _append_error(req["messages"][0]["content"], error)
            raw = await self._obtain(req, dry)
            try:
                value = output.model_validate(raw)
                if validate:
                    validate(value)
                return value
            except (ValidationError, ValueError) as e:
                error = str(e)
                self._forget(req)
                log.info("%s output failed validation (attempt %d): %s", phase, attempt + 1, error)
        raise LLMSchemaError(f"{phase}: output failed schema validation twice: {error}")

    async def _obtain(self, req: dict[str, Any], dry: Callable[[], LLMModel]) -> dict[str, Any]:
        key = self.cache_key(req if self.mode != "dry" else {"dry": True, **req})
        cached = self.cache_dir / f"{key}.json"
        if cached.exists():
            self.usage.cache_hits += 1
            result: dict[str, Any] = json.loads(cached.read_text(encoding="utf-8"))
            return result
        if self.mode == "dry":
            raw = dry().model_dump(mode="json", by_alias=True)
        elif self.mode == "replay":
            rec = self.fixtures_dir / f"{key}.json"
            if not rec.exists():
                raise LLMReplayMissing(f"no recorded LLM response for key {key} (expected {rec})")
            raw = json.loads(rec.read_text(encoding="utf-8"))
        else:
            raw = await self._call_api(req)
            if self.mode == "record":
                self.fixtures_dir.mkdir(parents=True, exist_ok=True)
                (self.fixtures_dir / f"{key}.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        self.usage.generated += 1
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return raw

    def _forget(self, req: dict[str, Any]) -> None:
        """An invalid output must not stay cached."""
        for k in (self.cache_key(req), self.cache_key({"dry": True, **req})):
            p = self.cache_dir / f"{k}.json"
            if p.exists():
                p.unlink()

    async def _call_api(self, req: dict[str, Any]) -> dict[str, Any]:
        import anthropic

        client = self.client()
        tool_name = req["tools"][0]["name"]
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with self.sem:
                    self.usage.calls += 1
                    resp = await client.messages.create(**req)
            except anthropic.APIStatusError as e:
                status = e.status_code
                if not (status == 429 or status >= 500) or attempt == MAX_RETRIES:
                    raise LLMCallError(f"API error {status}: {e}") from e
                delay = RETRY_BASE_SECONDS * (2**attempt)
            except anthropic.APIConnectionError as e:  # includes APITimeoutError
                if attempt == MAX_RETRIES:
                    raise LLMCallError(f"connection error: {e}") from e
                delay = RETRY_BASE_SECONDS * (2**attempt)
            else:
                self.usage.add(req["model"], getattr(resp, "usage", None))
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                        data = block.input
                        return dict(data) if not isinstance(data, str) else json.loads(data)
                return {}
            log.info("retrying LLM call in %.1fs (attempt %d)", delay, attempt + 1)
            await self._sleep(delay)
        raise LLMCallError("unreachable")  # pragma: no cover


def _append_error(content: Any, error: str) -> Any:
    note = "\n\nYour previous output failed validation. Fix these errors and call the tool again:\n" + error
    if isinstance(content, str):
        return content + note
    return [*content, {"type": "text", "text": note}]


def image_block(data_b64: str, media_type: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


def ceil_div(a: int, b: int) -> int:
    return math.ceil(a / b)
