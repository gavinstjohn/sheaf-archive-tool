from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProtocolMaturity(str, Enum):
    DRAFT = "draft"
    PROBATIONARY = "probationary"
    TRUSTED = "trusted"


@dataclass
class EnrichmentChainEntry:
    protocol_name: str
    required: bool = True


# ---------------------------------------------------------------------------
# Shape — structural pattern descriptor (not a protocol, but stored alongside)
# ---------------------------------------------------------------------------

@dataclass
class Shape:
    """Structural pattern descriptor — cheap filesystem-level matching.

    Shapes describe what a source *looks like* on disk without inspecting
    content. They gate which identification protocols run.
    """
    name: str
    description: str
    # List of indicator dicts evaluated by the classifier.
    # Supported keys:
    #   dcim_layout: bool          — has a DCIM/ subdirectory
    #   all_same_extension: [...]  — 90%+ of files share one of these extensions
    #   extension: [...]           — single-file shape; file has one of these exts
    #   min_file_count: int        — at least N files
    #   max_file_count: int        — at most N files
    #   max_depth: int             — directory depth (1 = flat)
    #   filename_pattern: str      — "sequential", "dated", or "sequential_or_dated"
    #   has_subdirectories: bool   — contains subdirectories
    indicators: list[dict] = field(default_factory=list)
    # If True, this source should be decomposed into sub-units before identification
    is_container: bool = False


def shape_from_dict(data: dict) -> Shape:
    return Shape(
        name=data["name"],
        description=data.get("description", ""),
        indicators=data.get("indicators", []),
        is_container=data.get("is_container", False),
    )


def shape_to_dict(shape: Shape) -> dict:
    return {
        "name": shape.name,
        "description": shape.description,
        "indicators": shape.indicators,
        "is_container": shape.is_container,
    }


# ---------------------------------------------------------------------------
# Protocol base
# ---------------------------------------------------------------------------

@dataclass
class Protocol:
    """Common envelope fields shared by all protocol types."""
    name: str
    type: str                        # "import", "enrichment", or "identification"
    version: str
    created: str                     # ISO-8601 date
    maturity: ProtocolMaturity
    description: str
    confidence_threshold: float | None = None  # overrides global if set


# ---------------------------------------------------------------------------
# Identification protocol
# ---------------------------------------------------------------------------

@dataclass
class IdentificationProtocol(Protocol):
    """Fires on structural shapes; classifies content semantically.

    Returns a classification string (e.g. "camera-roll", "scanned-notebook")
    that import protocols declare they accept.
    """
    # Which shapes trigger this identification protocol
    # e.g. [{shape: image_sequence}, {shape: dcim_directory}]
    triggers: list[dict] = field(default_factory=list)
    # The semantic classification this protocol produces
    classification: str = ""
    # How to perform identification:
    #   "heuristic" — shape match alone is sufficient; no content inspection
    #   "claude"    — inspect sampled files via the Claude API
    method: str = "heuristic"
    instructions: str = ""


# ---------------------------------------------------------------------------
# Import protocol
# ---------------------------------------------------------------------------

@dataclass
class ImportProtocol(Protocol):
    """Import protocol — source-specific, learns how to bring files into the archive."""

    # --- New-style: accepts a semantic classification from the identification layer ---
    accepts_classification: str = ""

    # --- Old-style trigger conditions (backward compatibility) ---
    # e.g. [{extension: [.jpg, .jpeg]}, {dcim_layout: true}]
    triggers: list[dict] = field(default_factory=list)

    # Extensions to import — all others are skipped. Empty list = import everything.
    # e.g. [".jpg", ".jpeg", ".mts"]
    include_extensions: list[str] = field(default_factory=list)

    # Ordered enrichment steps to run after import
    enrichment_chain: list[EnrichmentChainEntry] = field(default_factory=list)
    # Archive placement templates (protocol-defined, filled in during learning flow)
    category_template: str = ""
    subcategory_template: str | None = None
    filename_template: str = ""
    # Any additional protocol-specific instructions for the executor/model
    instructions: str = ""


# ---------------------------------------------------------------------------
# Enrichment protocol
# ---------------------------------------------------------------------------

@dataclass
class EnrichmentProtocol(Protocol):
    """Enrichment protocol — media-type-general, processes files already in the archive."""
    media_types: list[str] = field(default_factory=list)
    output_fields: list[str] = field(default_factory=list)
    instructions: str = ""
    # Execution method: "command" or "claude" (default: "command")
    # "command" is the universal local method — command_template can invoke any tool.
    method: str = "command"
    # method=command: shell command template; variables: {file_path}, {archive_root}, {sidecar_path}
    command_template: str = ""


# ---------------------------------------------------------------------------
# Runtime statistics
# ---------------------------------------------------------------------------

@dataclass
class ProtocolRunStats:
    """Runtime statistics stored alongside a protocol, tracked across runs."""
    run_count: int = 0
    last_run: str | None = None      # ISO-8601 datetime
    last_file_count: int = 0
    success_count: int = 0           # runs without error


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def protocol_from_dict(data: dict) -> ImportProtocol | EnrichmentProtocol | IdentificationProtocol:
    """Construct a typed Protocol from a parsed YAML dict."""
    ptype = data.get("type", "")
    maturity = ProtocolMaturity(data.get("maturity", "draft"))

    common = dict(
        name=data["name"],
        type=ptype,
        version=str(data.get("version", "1")),
        created=data.get("created", ""),
        maturity=maturity,
        description=data.get("description", ""),
        confidence_threshold=data.get("confidence_threshold"),
    )

    if ptype == "identification":
        return IdentificationProtocol(
            **common,
            triggers=data.get("triggers", []),
            classification=data.get("classification", ""),
            method=data.get("method", "heuristic"),
            instructions=data.get("instructions", ""),
        )

    elif ptype == "import":
        chain = [
            EnrichmentChainEntry(
                protocol_name=e if isinstance(e, str) else e["protocol_name"],
                required=e.get("required", True) if isinstance(e, dict) else True,
            )
            for e in data.get("enrichment_chain", [])
        ]
        return ImportProtocol(
            **common,
            accepts_classification=data.get("accepts_classification", ""),
            triggers=data.get("triggers", []),
            include_extensions=data.get("include_extensions", []),
            enrichment_chain=chain,
            category_template=data.get("category_template", ""),
            subcategory_template=data.get("subcategory_template"),
            filename_template=data.get("filename_template", ""),
            instructions=data.get("instructions", ""),
        )

    elif ptype == "enrichment":
        return EnrichmentProtocol(
            **common,
            media_types=data.get("media_types", []),
            output_fields=data.get("output_fields", []),
            instructions=data.get("instructions", ""),
            method=data.get("method", "command"),
            command_template=data.get("command_template", ""),
        )

    else:
        raise ValueError(f"Unknown protocol type: {ptype!r}")


def protocol_to_dict(protocol: ImportProtocol | EnrichmentProtocol | IdentificationProtocol) -> dict:
    """Serialize a protocol to a plain dict suitable for YAML output."""
    d: dict[str, Any] = {
        "name": protocol.name,
        "type": protocol.type,
        "version": protocol.version,
        "created": protocol.created,
        "maturity": protocol.maturity.value,
        "description": protocol.description,
    }
    if protocol.confidence_threshold is not None:
        d["confidence_threshold"] = protocol.confidence_threshold

    if isinstance(protocol, IdentificationProtocol):
        d["triggers"] = protocol.triggers
        d["classification"] = protocol.classification
        d["method"] = protocol.method
        if protocol.instructions:
            d["instructions"] = protocol.instructions

    elif isinstance(protocol, ImportProtocol):
        if protocol.accepts_classification:
            d["accepts_classification"] = protocol.accepts_classification
        if protocol.triggers:
            d["triggers"] = protocol.triggers
        if protocol.include_extensions:
            d["include_extensions"] = protocol.include_extensions
        d["category_template"] = protocol.category_template
        if protocol.subcategory_template is not None:
            d["subcategory_template"] = protocol.subcategory_template
        d["filename_template"] = protocol.filename_template
        d["enrichment_chain"] = [
            {"protocol_name": e.protocol_name, "required": e.required}
            for e in protocol.enrichment_chain
        ]
        if protocol.instructions:
            d["instructions"] = protocol.instructions

    elif isinstance(protocol, EnrichmentProtocol):
        d["media_types"] = protocol.media_types
        d["output_fields"] = protocol.output_fields
        d["method"] = protocol.method
        if protocol.instructions:
            d["instructions"] = protocol.instructions
        if protocol.command_template:
            d["command_template"] = protocol.command_template

    return d
