"""Source ingestion: discovery and fetching behind one adapter interface."""

from newsletter.ingestion.base import (
    AdapterError,
    DiscoveryError,
    FetchError,
    IngestionResult,
    SourceAdapter,
    SourceOutcome,
    UnsupportedStrategyError,
    build_adapter,
    ingest_all,
    ingest_source,
)
from newsletter.ingestion.http import HttpClient, HttpError, HttpResponse, UrllibHttpClient

__all__ = [
    "AdapterError",
    "DiscoveryError",
    "FetchError",
    "HttpClient",
    "HttpError",
    "HttpResponse",
    "IngestionResult",
    "SourceAdapter",
    "SourceOutcome",
    "UnsupportedStrategyError",
    "UrllibHttpClient",
    "build_adapter",
    "ingest_all",
    "ingest_source",
]
