"""Weekly Intelligence Newspaper engine.

Deterministic Python owns control flow; OpenAI is used only for narrow,
schema-constrained semantic judgment. See CLAUDE.md for the architecture rules.
"""

__version__ = "0.1.0"

#: Bumped whenever a model-facing schema changes; part of the assessment cache key.
SCHEMA_VERSION = "1"
