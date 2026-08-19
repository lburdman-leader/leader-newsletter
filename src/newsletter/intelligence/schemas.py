"""Wire schemas for model responses.

There are deliberately **two** models for one concept:

``AssessmentPayload``
    What the model is asked to return. Its JSON Schema must stay inside the
    subset OpenAI strict Structured Outputs accepts, which excludes ``minimum``,
    ``maximum``, ``maxItems``, ``pattern`` and friends. Numeric ranges are
    therefore *described* to the model, not encoded as schema constraints.

``ArticleAssessment`` (in ``newsletter.models``)
    What the pipeline is allowed to use. Every bound is enforced here, in
    Python, after the response arrives.

That split is the architecture in miniature: the model is *asked* for a 0-5
rating; the software *guarantees* it. A response that violates the rubric is
rejected and retried rather than quietly clamped.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from newsletter.models import ArticleAssessment, TopicCategory

#: Bumped whenever this wire schema changes shape. Part of the cache identity,
#: so a schema change can never reuse an assessment produced by the old one.
ASSESSMENT_SCHEMA_VERSION = "2"

#: Keywords OpenAI strict Structured Outputs does not accept. Asserted in tests.
UNSUPPORTED_SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "contains",
        "minProperties",
        "maxProperties",
        "patternProperties",
        "propertyNames",
    }
)

#: Extra facts beyond this are trimmed: cosmetic, no semantic loss.
MAX_KEY_FACTS = 8


class SchemaViolation(Exception):
    """A structurally valid response that breaks the rubric (e.g. a rating of 9)."""


class AssessmentPayload(BaseModel):
    """Exactly what the analyzer model may return -- nothing more.

    Note what is absent: no score, no publish/reject decision, no URL, no date
    the pipeline will trust for filtering. The model contributes judgment; the
    wrapper keeps control.
    """

    model_config = ConfigDict(extra="forbid")

    category: TopicCategory = Field(
        description=(
            "The single best-fitting category from the closed taxonomy. "
            "Use 'other' when nothing fits; never invent a category."
        )
    )
    topic_relevance: int = Field(
        description="Integer 0-5. How squarely this fits the newsletter's themes."
    )
    business_impact: int = Field(
        description="Integer 0-5. Concrete consequences for an enterprise operator."
    )
    novelty: int = Field(description="Integer 0-5. How new this is versus already known.")
    actionability: int = Field(
        description="Integer 0-5. Whether a reader could act on it this week."
    )
    confidence: float = Field(
        description="Float 0.0-1.0. Your confidence given the evidence in the article text."
    )
    summary: str = Field(
        description=(
            "En español neutro: 2-3 frases estrictamente factuales, tomadas solo del "
            "artículo. Sin interpretación."
        )
    )
    why_it_matters: str = Field(
        description=(
            "En español neutro: 1-2 frases sobre qué significa esto para una empresa que "
            "hace contenido infantil en YouTube y produce con IA."
        )
    )
    key_facts: list[str] = Field(
        description="En español neutro: hasta 8 datos breves y verificables del artículo."
    )
    event_subject: str | None = Field(
        description="Who or what acted, e.g. 'OpenAI'. Null if the article has no single actor."
    )
    event_action: str | None = Field(description="What they did, e.g. 'released'. Null if unclear.")
    event_object: str | None = Field(
        description="What it was done to, e.g. 'GPT-5 API'. Null if unclear."
    )
    event_date: str | None = Field(
        description="Date of the event as YYYY-MM-DD if the article states one, else null."
    )

    def to_assessment(self) -> ArticleAssessment:
        """Validate into the domain model, enforcing every bound in Python.

        Raises :class:`SchemaViolation` when the model returned a structurally
        valid response that breaks the rubric.
        """
        for name in ("topic_relevance", "business_impact", "novelty", "actionability"):
            value = getattr(self, name)
            if not 0 <= value <= 5:
                raise SchemaViolation(f"{name}={value} is outside the 0-5 rubric")
        if not 0.0 <= self.confidence <= 1.0:
            raise SchemaViolation(f"confidence={self.confidence} is outside 0.0-1.0")
        if not self.summary.strip():
            raise SchemaViolation("summary is empty")
        if not self.why_it_matters.strip():
            raise SchemaViolation("why_it_matters is empty")

        facts = [fact.strip() for fact in self.key_facts if fact and fact.strip()]

        return ArticleAssessment(
            category=self.category,
            topic_relevance=self.topic_relevance,
            business_impact=self.business_impact,
            novelty=self.novelty,
            actionability=self.actionability,
            confidence=self.confidence,
            summary=self.summary.strip(),
            why_it_matters=self.why_it_matters.strip(),
            key_facts=facts[:MAX_KEY_FACTS],
            event_subject=_clean(self.event_subject),
            event_action=_clean(self.event_action),
            event_object=_clean(self.event_object),
            event_date=_clean(self.event_date),
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def find_unsupported_keywords(schema: dict) -> set[str]:
    """Every strict-mode-unsupported keyword present anywhere in ``schema``."""
    found: set[str] = set()
    stack: list[object] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.update(UNSUPPORTED_SCHEMA_KEYWORDS.intersection(node))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found
