"""Narrow semantic services: strict schemas in, validated judgment out."""

from newsletter.intelligence.analyzer import ANALYZER_PROMPT_VERSION, ArticleAnalyzer
from newsletter.intelligence.client import (
    ModelContractError,
    ModelError,
    ModelRefusal,
    ModelTimeout,
    ModelUnavailable,
    StructuredClient,
    build_openai_client,
)
from newsletter.intelligence.schemas import ASSESSMENT_SCHEMA_VERSION, AssessmentPayload

__all__ = [
    "ANALYZER_PROMPT_VERSION",
    "ASSESSMENT_SCHEMA_VERSION",
    "ArticleAnalyzer",
    "AssessmentPayload",
    "ModelContractError",
    "ModelError",
    "ModelRefusal",
    "ModelTimeout",
    "ModelUnavailable",
    "StructuredClient",
    "build_openai_client",
]
