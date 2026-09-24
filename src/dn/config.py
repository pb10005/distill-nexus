# @covers AC-002, AC-003, AC-004, AC-005
"""Project configuration (``.dn/config.yaml``) and ``taxonomy.yaml`` loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dn.errors import ConfigError

# @assumption AS-001
DN_DIR = ".dn"
CONFIG_NAME = "config.yaml"
TAXONOMY_NAME = "taxonomy.yaml"

# @assumption AS-003
DEFAULT_MODEL = "claude-sonnet-4-6"


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_tokens: int = Field(default=800, ge=50, le=8000)
    overlap_ratio: float = Field(default=0.15, ge=0, lt=0.9)
    index_token_budget: int = Field(default=20000, ge=1000)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = DEFAULT_MODEL
    synth_model: str | None = None
    concurrency: int = Field(default=6, ge=1, le=32)
    confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    max_file_mb: float = Field(default=100, gt=0)
    lang: Literal["ja", "en", "auto"] = "auto"
    rename: bool = False
    dedupe: Literal["move", "trash", "keep"] = "move"
    exclude: list[str] = Field(default_factory=list)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)

    @property
    def effective_synth_model(self) -> str:
        return self.synth_model or self.model


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = ""
    description: str = ""
    examples: list[str] = Field(default_factory=list)
    subcategories: list[str] = Field(default_factory=list)

    @field_validator("subcategories")
    @classmethod
    def _sub_slugs(cls, v: list[str]) -> list[str]:
        import re

        for s in v:
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", s):
                raise ValueError(f"subcategory must be a kebab-case slug: {s!r}")
        return v


class Taxonomy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[Category] = Field(min_length=1)

    @field_validator("categories")
    @classmethod
    def _unique(cls, v: list[Category]) -> list[Category]:
        seen: set[str] = set()
        for c in v:
            if c.slug in seen:
                raise ValueError(f"duplicate slug: {c.slug}")
            if c.slug == "misc":
                raise ValueError("'misc' is reserved and must not be declared")
            seen.add(c.slug)
        return v

    def get(self, slug: str) -> Category | None:
        return next((c for c in self.categories if c.slug == slug), None)

    @property
    def slugs(self) -> list[str]:
        return [c.slug for c in self.categories]


def _format_validation(where: str, err: ValidationError) -> str:
    lines = [f"{where}: invalid configuration"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"])
        lines.append(f"  - {loc}: {e['msg']}")
    return "\n".join(lines)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML parse error: {e}") from e


def load_config(root: Path) -> Config:
    path = root / DN_DIR / CONFIG_NAME
    if not path.exists():
        return Config()
    data = _read_yaml(path)
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation(str(path), e)) from e


def load_taxonomy(root: Path) -> Taxonomy | None:
    path = root / TAXONOMY_NAME
    if not path.exists():
        return None
    data = _read_yaml(path)
    try:
        return Taxonomy.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation(str(path), e)) from e


CONFIG_TEMPLATE = """\
# Distill Nexus project configuration
model: claude-sonnet-4-6
synth_model: claude-opus-4-1        # used only by synthesize; defaults to model when omitted
concurrency: 6
confidence_threshold: 0.6
max_file_mb: 100
lang: auto                          # ja | en | auto
rename: false
dedupe: move                        # move | trash | keep
exclude:
  - "*.tmp"
  - "backup/**"
knowledge:
  chunk_tokens: 800
  overlap_ratio: 0.15
  index_token_budget: 20000
"""

TAXONOMY_TEMPLATE = """\
# Categories used for classification. 'misc' is implicit and must not be declared.
# Generate a proposal from your files with: dn plan --propose-taxonomy
categories:
  - slug: contracts
    name: 契約・法務
    description: NDA、業務委託契約、利用規約など法的効力のある文書
    examples: ["NDA_xxx.pdf", "業務委託基本契約書.docx"]
    subcategories: [nda, outsourcing, terms]
  - slug: specs
    name: 仕様・設計
    description: 要件定義、API 仕様、設計書、ADR
"""


def init_project(root: Path) -> list[Path]:
    """Create the templates; never overwrite existing files. Returns created paths."""
    created: list[Path] = []
    targets = [(root / DN_DIR / CONFIG_NAME, CONFIG_TEMPLATE), (root / TAXONOMY_NAME, TAXONOMY_TEMPLATE)]
    for path, content in targets:
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        created.append(path)
    return created
