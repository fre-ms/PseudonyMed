"""Real redaction of PDF pages, document clean-up and optional rasterising."""

from pseudonymed.redaction.pdf import (
    ALLOWED_METADATA,
    PageResult,
    RedactionOptions,
    layout_intact,
    rasterise_document,
    rasterise_page,
    redact_page,
    sanitise,
    save,
)

__all__ = [
    "ALLOWED_METADATA",
    "RedactionOptions",
    "PageResult",
    "sanitise",
    "layout_intact",
    "rasterise_document",
    "rasterise_page",
    "redact_page",
    "save",
]
