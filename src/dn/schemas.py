# @covers AC-010, AC-028, AC-033, AC-041, AC-043, AC-056
"""All pydantic models used by dn, including the LLM output schemas.

LLM output models double as tool ``input_schema`` sources (``tool_schema()``),
so the schema the model is forced to fill and the schema we re-validate with
are the same object.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------- inventory / extract


class InventoryEntry(BaseModel):
    path: str  # root-relative POSIX path, original code points
    size: int
    mtime_ns: int
    hash: str | None = None
    hash_mode: Literal["head4m", "full"] | None = None
    ext: str = ""
    skipped: str | None = None  # e.g. "too_large"


class ExtractMeta(BaseModel):
    source: str
    type: str
    pages: int | None = None
    chars: int = 0
    lang: str = "und"
    quality: Literal["text", "ocr", "mixed", "vision", "none"] = "text"
    truncated: bool = False


# ---------------------------------------------------------------- LLM outputs


class LLMModel(BaseModel):
    """Base for tool_use outputs. ``tool_schema`` is what the model must fill."""

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def tool_schema(cls, **overrides: Any) -> dict[str, Any]:
        schema = cls.model_json_schema()
        schema.pop("title", None)
        return schema


class Label(LLMModel):
    category: str
    subcategory: str | None = None
    confidence: float = Field(ge=0, le=1)
    title: str = Field(max_length=80)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    summary: str = Field(max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=8)
    has_domain_knowledge: bool = True

    @classmethod
    def tool_schema(cls, **overrides: Any) -> dict[str, Any]:
        """§6.1: category enum is generated from the taxonomy slugs + misc."""
        categories: list[str] = list(overrides.get("categories", []))
        enum = [c for c in categories if c != "misc"] + ["misc"]
        return {
            "type": "object",
            "required": ["category", "confidence", "title", "summary", "tags"],
            "properties": {
                "category": {"type": "string", "enum": enum},
                "subcategory": {"type": ["string", "null"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "title": {"type": "string", "maxLength": 80},
                "date": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                "summary": {"type": "string", "maxLength": 200},
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "has_domain_knowledge": {"type": "boolean"},
            },
        }


class ProposedCategory(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    description: str
    examples: list[str] = Field(default_factory=list)


class TaxonomyProposal(LLMModel):
    categories: list[ProposedCategory] = Field(min_length=5, max_length=15)

    @field_validator("categories")
    @classmethod
    def _no_misc(cls, v: list[ProposedCategory]) -> list[ProposedCategory]:
        if any(c.slug == "misc" for c in v):
            raise ValueError("misc must not be proposed")
        if len({c.slug for c in v}) != len(v):
            raise ValueError("duplicate slug")
        return v


class VisionResult(LLMModel):
    transcript: str
    description: str


class Term(BaseModel):
    term: str
    definition: str
    aliases: list[str] = Field(default_factory=list)
    locator: str = ""


EntityType = Literal["person", "org", "product", "system", "concept", "process", "other"]


class Entity(BaseModel):
    name: str
    type: EntityType = "other"
    description: str = ""
    locator: str = ""


class Fact(BaseModel):
    statement: str
    subject: str
    predicate: str
    object: str
    confidence: float = Field(default=0.8, ge=0, le=1)
    locator: str = ""


class Relation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    relation: str
    to: str
    locator: str = ""


class Question(BaseModel):
    question: str
    why: str = ""


class Distilled(LLMModel):
    terms: list[Term] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)

    @classmethod
    def tool_schema(cls, **overrides: Any) -> dict[str, Any]:
        schema = cls.model_json_schema(by_alias=True)
        schema.pop("title", None)
        return schema


class SamePair(BaseModel):
    a: str
    b: str
    same: bool
    canonical: str


class SamePairs(LLMModel):
    pairs: list[SamePair] = Field(default_factory=list)


class TopicSpec(BaseModel):
    slug: str
    name: str
    description: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class Topics(LLMModel):
    topics: list[TopicSpec] = Field(default_factory=list)


class MarkdownDoc(LLMModel):
    markdown: str


# ---------------------------------------------------------------- source-attached records


class Source(BaseModel):
    path: str
    hash: str
    locator: str = ""


class FactsFile(BaseModel):
    """Contents of ``facts/<hash>.json``: every element carries a ``source``."""

    hash: str
    path: str
    terms: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)


class LabelRecord(BaseModel):
    hash: str
    path: str
    label: Label
    llm_category: str | None = None  # category before the confidence threshold


# ---------------------------------------------------------------- plan / manifest

Op = Literal["move", "copy", "trash", "skip", "noop"]


class PlanEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str | None
    hash: str
    hash_mode: str | None = None
    op: Op
    category: str
    confidence: float
    reason: str = ""


class Plan(BaseModel):
    version: int = 1
    root: str
    out_root: str
    dedupe: Literal["move", "trash", "keep"] = "move"
    entries: list[PlanEntry] = Field(default_factory=list)


class Move(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str | None
    hash: str
    hash_mode: str | None = None
    op: Op


class ManifestRun(BaseModel):
    run_id: str
    out_root: str
    moves: list[Move] = Field(default_factory=list)
    undone_at: str | None = None


class Manifest(BaseModel):
    version: int = 1
    root: str
    runs: list[ManifestRun] = Field(default_factory=list)
