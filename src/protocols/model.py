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


@dataclass
class Protocol:
    """Common envelope fields shared by all protocol types."""
    name: str
    type: str                        # "import" or "enrichment"
    version: str
    created: str                     # ISO-8601 date
    maturity: ProtocolMaturity
    description: str
    confidence_threshold: float | None = None  # overrides global if set


@dataclass
class ImportProtocol(Protocol):
    """Import protocol — source-specific, learns how to bring files into the archive."""
    # Trigger conditions: list of dicts, e.g. [{extension: [.jpg, .jpeg]}, {dcim_layout: true}]
    triggers: list[dict] = field(default_factory=list)
    # Ordered enrichment steps to run after import
    enrichment_chain: list[EnrichmentChainEntry] = field(default_factory=list)
    # Archive placement templates (protocol-defined, filled in during learning flow)
    # These are plain strings the executor evaluates at runtime.
    category_template: str = ""
    subcategory_template: str | None = None
    filename_template: str = ""
    # Any additional protocol-specific instructions for the executor/model
    instructions: str = ""


@dataclass
class EnrichmentProtocol(Protocol):
    """Enrichment protocol — media-type-general, processes files already in the archive."""
    media_types: list[str] = field(default_factory=list)
    output_fields: list[str] = field(default_factory=list)
    instructions: str = ""
    # Execution method: "command", "ollama", or "claude" (default: "command")
    method: str = "command"
    # method=command: shell command template; variables: {file_path}, {archive_root}, {sidecar_path}
    command_template: str = ""
    # method=ollama: model name (e.g. "llava", "llama3.2-vision")
    ollama_model: str = ""
    # method=ollama: optional base URL override (default: http://localhost:11434)
    ollama_url: str = ""


@dataclass
class ProtocolRunStats:
    """Runtime statistics stored alongside a protocol, tracked across runs."""
    run_count: int = 0
    last_run: str | None = None      # ISO-8601 datetime
    last_file_count: int = 0
    success_count: int = 0           # runs without error


def protocol_from_dict(data: dict) -> ImportProtocol | EnrichmentProtocol:
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

    if ptype == "import":
        chain = [
            EnrichmentChainEntry(
                protocol_name=e if isinstance(e, str) else e["protocol_name"],
                required=e.get("required", True) if isinstance(e, dict) else True,
            )
            for e in data.get("enrichment_chain", [])
        ]
        return ImportProtocol(
            **common,
            triggers=data.get("triggers", []),
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
            ollama_model=data.get("ollama_model", ""),
            ollama_url=data.get("ollama_url", ""),
        )
    else:
        raise ValueError(f"Unknown protocol type: {ptype!r}")


def protocol_to_dict(protocol: ImportProtocol | EnrichmentProtocol) -> dict:
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

    if isinstance(protocol, ImportProtocol):
        d["triggers"] = protocol.triggers
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
        if protocol.ollama_model:
            d["ollama_model"] = protocol.ollama_model
        if protocol.ollama_url:
            d["ollama_url"] = protocol.ollama_url

    return d
