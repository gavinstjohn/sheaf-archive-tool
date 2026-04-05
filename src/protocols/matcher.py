"""matcher.py — Match a source path to an import protocol.

Two matching strategies:

1. **Classification-based** (new style): Run the shape → identification pipeline
   to get a semantic classification (e.g. "camera-roll"), then find import protocols
   that declare `accepts_classification: camera-roll`.

2. **Trigger-based** (legacy): Ask the model to score all protocols against the source
   using extension/trigger metadata. Used as a fallback when no shapes are defined or
   when old-style protocols lack `accepts_classification`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..adapter.base import BaseAdapter, Message
from .model import ImportProtocol

log = logging.getLogger(__name__)

_MAX_SOURCE_FILES = 30   # files to sample when describing the source to the model


@dataclass
class ProtocolMatch:
    protocol: ImportProtocol
    confidence: float      # 0.0 – 1.0
    reasoning: str
    # If matched via classification pipeline, the classification string
    classification: str | None = None


def match_by_classification(
    classification: str,
    import_protocols: dict[str, ImportProtocol],
    confidence: float = 1.0,
    reasoning: str = "",
) -> list[ProtocolMatch]:
    """Find import protocols that accept a given semantic classification.

    Returns protocols with `accepts_classification` matching the classification string,
    ordered by maturity (trusted first) then name.
    """
    from .model import ProtocolMaturity
    maturity_order = {
        ProtocolMaturity.TRUSTED: 0,
        ProtocolMaturity.PROBATIONARY: 1,
        ProtocolMaturity.DRAFT: 2,
    }
    matches = [
        ProtocolMatch(
            protocol=p,
            confidence=confidence,
            reasoning=reasoning or f"Accepts classification '{classification}'",
            classification=classification,
        )
        for p in import_protocols.values()
        if p.accepts_classification == classification
    ]
    return sorted(matches, key=lambda m: (maturity_order.get(m.protocol.maturity, 3), m.protocol.name))


def match_protocols(
    source_path: Path,
    protocols: dict[str, ImportProtocol],
    adapter: BaseAdapter,
    confidence_threshold: float = 0.75,
) -> list[ProtocolMatch]:
    """Legacy trigger-based matching: ask the model to score protocols against the source.

    Returns matches sorted by confidence (highest first).
    Returns an empty list if there are no protocols to match against.

    Only protocols with old-style `triggers` are considered here; protocols that use
    `accepts_classification` are matched via match_by_classification() instead.
    """
    # Filter to old-style protocols (those with triggers, no accepts_classification)
    trigger_protocols = {
        name: p for name, p in protocols.items()
        if p.triggers and not p.accepts_classification
    }
    if not trigger_protocols:
        return []

    source_summary = _summarise_source(source_path)
    protocol_descriptions = _describe_protocols(trigger_protocols)

    prompt = f"""You are evaluating whether any known import protocols match a media source.

Source path: {source_path}
Source contents:
{source_summary}

Known protocols:
{protocol_descriptions}

For each protocol, assign a confidence score (0.0 to 1.0) indicating how well it matches this source.
- 1.0 = certain match (triggers clearly satisfied by the source files)
- 0.0 = definitely not a match

Respond with a JSON array only, no other text:
[
  {{"name": "<protocol_name>", "confidence": 0.95, "reasoning": "<one sentence>"}},
  ...
]"""

    response = adapter.chat(
        [Message(role="user", content=prompt)],
        system="You are a media archive assistant. Respond only with the requested JSON.",
    )

    try:
        raw = response.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        scores = json.loads(raw)
    except (json.JSONDecodeError, IndexError) as e:
        log.warning("Could not parse protocol match response: %s\n%s", e, response.content)
        return []

    matches = []
    for item in scores:
        name = item.get("name")
        if name not in trigger_protocols:
            continue
        matches.append(ProtocolMatch(
            protocol=trigger_protocols[name],
            confidence=float(item.get("confidence", 0.0)),
            reasoning=item.get("reasoning", ""),
        ))

    return sorted(matches, key=lambda m: m.confidence, reverse=True)


def _summarise_source(source_path: Path) -> str:
    """Produce a text summary of the source directory for the model."""
    lines = []
    count = 0
    ext_counts: dict[str, int] = {}

    if source_path.is_file():
        return f"Single file: {source_path.name}  ({source_path.stat().st_size:,} bytes)"

    for p in sorted(source_path.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith("._") or p.name == ".DS_Store":
            continue
        ext = p.suffix.lower() or "(no ext)"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        if count < _MAX_SOURCE_FILES:
            rel = p.relative_to(source_path)
            lines.append(f"  {rel}  ({p.stat().st_size:,} bytes)")
        count += 1

    summary = "\n".join(lines)
    if count > _MAX_SOURCE_FILES:
        summary += f"\n  ... and {count - _MAX_SOURCE_FILES} more files"

    ext_summary = "  Extensions: " + ", ".join(
        f"{ext} ({n})" for ext, n in sorted(ext_counts.items(), key=lambda x: -x[1])
    )
    return summary + "\n" + ext_summary


def _describe_protocols(protocols: dict[str, ImportProtocol]) -> str:
    lines = []
    for name, p in protocols.items():
        lines.append(f"  Protocol: {name}")
        lines.append(f"    Description: {p.description}")
        lines.append(f"    Triggers: {p.triggers}")
        lines.append(f"    Category: {p.category_template}" +
                     (f" / {p.subcategory_template}" if p.subcategory_template else ""))
    return "\n".join(lines)
