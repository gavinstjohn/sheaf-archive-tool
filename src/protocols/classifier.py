"""classifier.py — Structural analysis, shape matching, and source classification.

The classification pipeline:
  1. analyze_structure(path)  → StructuralSummary
  2. match_shapes(summary, known_shapes)  → list of (Shape, confidence) pairs
  3. run_identification(path, shape, id_protocols, adapter, threshold)
       → ClassificationResult
  4. classify_source(...)  → list[ClassificationResult]

For mixed sources (is_container shapes), find_logical_groups() decomposes the
source into sub-units before classification.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..adapter.base import BaseAdapter
    from .model import IdentificationProtocol, Shape

log = logging.getLogger(__name__)

# Files/directories to skip during structural analysis
_SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_SKIP_PREFIXES = ("._",)

# Extension families for grouping
_EXTENSION_FAMILIES: dict[str, frozenset[str]] = {
    "image":     frozenset({".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".heic", ".heif",
                             ".webp", ".gif", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".dng",
                             ".rw2", ".orf", ".pef", ".srw"}),
    "video":     frozenset({".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".m4v",
                             ".wmv", ".flv", ".webm", ".3gp"}),
    "audio":     frozenset({".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".aiff",
                             ".opus", ".wma"}),
    "document":  frozenset({".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md"}),
    "scan":      frozenset({".pdf", ".tiff", ".tif"}),
}

_SEQUENTIAL_RE = re.compile(r'[A-Za-z_-]*\d{3,}')
_DATE_RE = re.compile(r'(?:19|20)\d{2}[-_]?\d{2}[-_]?\d{2}')


@dataclass
class StructuralSummary:
    """Cheap filesystem-level description of a source path."""
    path: Path
    is_file: bool
    # Counts of files by extension (lowercase)
    extension_counts: dict[str, int] = field(default_factory=dict)
    # Total non-hidden file count
    file_count: int = 0
    # Extensions covering ≥90% of files (by count)
    dominant_extensions: list[str] = field(default_factory=list)
    # Extension families present
    extension_families: list[str] = field(default_factory=list)
    # Directory structure
    has_dcim: bool = False
    max_depth: int = 0          # depth below source path (0 = file or empty dir)
    has_subdirectories: bool = False
    # Filename patterns detected
    filename_pattern: str | None = None   # "sequential", "dated", "sequential_or_dated", "mixed"
    # Sample filenames (for model-based identification)
    sample_filenames: list[str] = field(default_factory=list)


def analyze_structure(path: Path) -> StructuralSummary:
    """Build a StructuralSummary from the filesystem without reading file content."""
    if path.is_file():
        return StructuralSummary(
            path=path,
            is_file=True,
            extension_counts={path.suffix.lower(): 1} if path.suffix else {},
            file_count=1,
            dominant_extensions=[path.suffix.lower()] if path.suffix else [],
            extension_families=_families_for_extensions([path.suffix.lower()]),
            sample_filenames=[path.name],
        )

    ext_counts: dict[str, int] = {}
    max_depth = 0
    has_dcim = False
    has_subdirs = False
    all_filenames: list[str] = []

    for item in path.rglob("*"):
        # Skip hidden/system files
        if item.name in _SKIP_NAMES:
            continue
        if any(item.name.startswith(p) for p in _SKIP_PREFIXES):
            continue

        depth = len(item.relative_to(path).parts)

        if item.is_dir():
            has_subdirs = True
            if depth == 1:
                if item.name.upper() == "DCIM":
                    has_dcim = True
            max_depth = max(max_depth, depth)
            continue

        if not item.is_file():
            continue

        max_depth = max(max_depth, depth)
        ext = item.suffix.lower() or "(no ext)"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        if len(all_filenames) < 100:
            all_filenames.append(item.name)

    file_count = sum(ext_counts.values())
    dominant = _dominant_extensions(ext_counts, file_count)
    families = _families_for_extensions(dominant)
    pattern = _detect_filename_pattern(all_filenames)

    return StructuralSummary(
        path=path,
        is_file=False,
        extension_counts=ext_counts,
        file_count=file_count,
        dominant_extensions=dominant,
        extension_families=families,
        has_dcim=has_dcim,
        max_depth=max_depth,
        has_subdirectories=has_subdirs,
        filename_pattern=pattern,
        sample_filenames=all_filenames[:30],
    )


def _dominant_extensions(ext_counts: dict[str, int], total: int) -> list[str]:
    """Return extensions covering ≥90% of files, sorted by count desc."""
    if total == 0:
        return []
    threshold = total * 0.9
    running = 0
    result = []
    for ext, count in sorted(ext_counts.items(), key=lambda x: -x[1]):
        result.append(ext)
        running += count
        if running >= threshold:
            break
    return result


def _families_for_extensions(extensions: list[str]) -> list[str]:
    families = []
    for family, members in _EXTENSION_FAMILIES.items():
        if any(e in members for e in extensions):
            families.append(family)
    return families


def _detect_filename_pattern(filenames: list[str]) -> str | None:
    if not filenames:
        return None
    sequential = sum(1 for f in filenames if _SEQUENTIAL_RE.search(Path(f).stem))
    dated = sum(1 for f in filenames if _DATE_RE.search(f))
    total = len(filenames)
    frac_seq = sequential / total
    frac_dated = dated / total
    if frac_seq >= 0.7 and frac_dated >= 0.7:
        return "sequential_or_dated"
    if frac_dated >= 0.7:
        return "dated"
    if frac_seq >= 0.7:
        return "sequential"
    if frac_seq > 0.1 or frac_dated > 0.1:
        return "mixed"
    return None


# ---------------------------------------------------------------------------
# Shape matching
# ---------------------------------------------------------------------------

@dataclass
class ShapeMatch:
    shape: "Shape"
    confidence: float        # 0.0 – 1.0
    matched_indicators: int
    total_indicators: int


def match_shapes(
    summary: StructuralSummary,
    shapes: dict[str, "Shape"],
) -> list[ShapeMatch]:
    """Score each known shape against the structural summary.

    Returns matches with confidence > 0, sorted by confidence descending.
    """
    results = []
    for shape in shapes.values():
        confidence, matched, total = _score_shape(summary, shape)
        if confidence > 0:
            results.append(ShapeMatch(
                shape=shape,
                confidence=confidence,
                matched_indicators=matched,
                total_indicators=total,
            ))
    return sorted(results, key=lambda m: m.confidence, reverse=True)


def _score_shape(summary: StructuralSummary, shape: "Shape") -> tuple[float, int, int]:
    """Return (confidence, matched_count, total_count) for a shape against a summary."""
    indicators = shape.indicators
    if not indicators:
        return 0.0, 0, 0

    matched = 0
    total = len(indicators)

    for indicator in indicators:
        if _evaluate_indicator(indicator, summary):
            matched += 1

    confidence = matched / total if total > 0 else 0.0
    return confidence, matched, total


def _evaluate_indicator(indicator: dict, summary: StructuralSummary) -> bool:
    """Evaluate a single indicator dict against the structural summary."""
    for key, value in indicator.items():
        if key == "dcim_layout":
            if bool(value) != summary.has_dcim:
                return False

        elif key == "all_same_extension":
            # All dominant extensions must be in the allowed set
            allowed = {e.lower() for e in (value if isinstance(value, list) else [value])}
            if not summary.dominant_extensions:
                return False
            if not all(e in allowed for e in summary.dominant_extensions):
                return False

        elif key == "extension":
            # Single-file shape: the file's extension must be in the list
            allowed = {e.lower() for e in (value if isinstance(value, list) else [value])}
            if not summary.is_file:
                return False
            ext = list(summary.extension_counts.keys())[0] if summary.extension_counts else ""
            if ext not in allowed:
                return False

        elif key == "min_file_count":
            if summary.file_count < int(value):
                return False

        elif key == "max_file_count":
            if summary.file_count > int(value):
                return False

        elif key == "max_depth":
            if summary.max_depth > int(value):
                return False

        elif key == "has_subdirectories":
            if bool(value) != summary.has_subdirectories:
                return False

        elif key == "filename_pattern":
            # Accept if the detected pattern matches or is a superset
            # e.g. value="sequential_or_dated" matches "sequential", "dated", "sequential_or_dated"
            detected = summary.filename_pattern
            if detected is None:
                return False
            if value == "sequential_or_dated":
                if detected not in ("sequential", "dated", "sequential_or_dated"):
                    return False
            elif detected != value and detected != "sequential_or_dated":
                return False

        else:
            log.debug("Unknown indicator key %r — skipping", key)
            # Unknown indicators don't fail matching; they're just ignored

    return True


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    path: Path
    shape: "Shape | None"
    shape_confidence: float
    # The semantic classification string, or None if not identified
    classification: str | None
    identification_confidence: float
    id_protocol: "IdentificationProtocol | None"
    reasoning: str
    needs_user_input: bool = False


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------

def run_identification(
    path: Path,
    summary: StructuralSummary,
    shape_match: ShapeMatch,
    id_protocols: dict[str, "IdentificationProtocol"],
    adapter: "BaseAdapter",
    threshold: float,
) -> ClassificationResult:
    """Run identification protocols triggered by a shape match.

    Returns the best classification, or a result with needs_user_input=True
    if confidence is below threshold or no protocol matches.
    """
    # Find identification protocols that trigger on this shape
    triggered = [
        p for p in id_protocols.values()
        if any(t.get("shape") == shape_match.shape.name for t in p.triggers)
    ]

    if not triggered:
        return ClassificationResult(
            path=path,
            shape=shape_match.shape,
            shape_confidence=shape_match.confidence,
            classification=None,
            identification_confidence=0.0,
            id_protocol=None,
            reasoning=f"Shape '{shape_match.shape.name}' matched but no identification protocol is defined for it.",
            needs_user_input=True,
        )

    best: ClassificationResult | None = None

    for id_proto in triggered:
        result = _run_id_protocol(path, summary, shape_match, id_proto, adapter)
        if best is None or result.identification_confidence > best.identification_confidence:
            best = result

    assert best is not None
    if best.identification_confidence < threshold:
        best.needs_user_input = True
        best.reasoning += f" (confidence {best.identification_confidence:.2f} < threshold {threshold:.2f})"

    return best


def _run_id_protocol(
    path: Path,
    summary: StructuralSummary,
    shape_match: ShapeMatch,
    id_proto: "IdentificationProtocol",
    adapter: "BaseAdapter",
) -> ClassificationResult:
    """Execute a single identification protocol and return its result."""
    method = id_proto.method or "heuristic"

    if method == "heuristic":
        # Shape match confidence directly becomes identification confidence
        confidence = shape_match.confidence
        return ClassificationResult(
            path=path,
            shape=shape_match.shape,
            shape_confidence=shape_match.confidence,
            classification=id_proto.classification,
            identification_confidence=confidence,
            id_protocol=id_proto,
            reasoning=(
                f"Heuristic identification: shape '{shape_match.shape.name}' matched "
                f"({shape_match.matched_indicators}/{shape_match.total_indicators} indicators). "
                f"Classification: {id_proto.classification!r}."
            ),
        )

    elif method == "claude":
        return _run_claude_identification(path, summary, shape_match, id_proto, adapter)

    else:
        return ClassificationResult(
            path=path,
            shape=shape_match.shape,
            shape_confidence=shape_match.confidence,
            classification=None,
            identification_confidence=0.0,
            id_protocol=id_proto,
            reasoning=f"Unknown identification method: {method!r}",
            needs_user_input=True,
        )


def _run_claude_identification(
    path: Path,
    summary: StructuralSummary,
    shape_match: ShapeMatch,
    id_proto: "IdentificationProtocol",
    adapter: "BaseAdapter",
) -> ClassificationResult:
    """Use the Claude API to identify content semantically."""
    from ..adapter.base import Message

    sample_info = "\n".join(f"  {f}" for f in summary.sample_filenames[:20])
    ext_info = ", ".join(
        f"{ext} ({count})" for ext, count in
        sorted(summary.extension_counts.items(), key=lambda x: -x[1])
    )

    prompt = f"""You are identifying the semantic content of a media source.

Source path: {path}
Shape: {shape_match.shape.name} — {shape_match.shape.description}
File count: {summary.file_count}
Extensions: {ext_info}
Sample filenames:
{sample_info}

Identification protocol: {id_proto.name}
Target classification: {id_proto.classification}

{id_proto.instructions or ''}

Does this source match the classification "{id_proto.classification}"?
Respond with JSON only:
{{"classification": "{id_proto.classification}" or null, "confidence": 0.0-1.0, "reasoning": "one sentence"}}"""

    response = adapter.chat(
        [Message(role="user", content=prompt)],
        system="You are a media archive assistant. Respond only with the requested JSON.",
    )

    try:
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        classification = data.get("classification")
        confidence = float(data.get("confidence", 0.0))
        reasoning = data.get("reasoning", "")
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        log.warning("Could not parse identification response: %s", e)
        classification = None
        confidence = 0.0
        reasoning = f"Parse error: {e}"

    return ClassificationResult(
        path=path,
        shape=shape_match.shape,
        shape_confidence=shape_match.confidence,
        classification=classification,
        identification_confidence=confidence,
        id_protocol=id_proto,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Top-level classify_source
# ---------------------------------------------------------------------------

def classify_source(
    source_path: Path,
    shapes: dict[str, "Shape"],
    id_protocols: dict[str, "IdentificationProtocol"],
    adapter: "BaseAdapter",
    threshold: float = 0.75,
) -> list[ClassificationResult]:
    """Classify a source path using the shape → identification pipeline.

    Returns a list of ClassificationResult objects — one per logical unit.
    For container shapes, the source is decomposed into sub-units first.
    """
    summary = analyze_structure(source_path)

    if not shapes:
        # No shapes defined yet — signal that user input is needed
        return [ClassificationResult(
            path=source_path,
            shape=None,
            shape_confidence=0.0,
            classification=None,
            identification_confidence=0.0,
            id_protocol=None,
            reasoning="No shapes are defined yet. The learning flow will define a shape.",
            needs_user_input=True,
        )]

    shape_matches = match_shapes(summary, shapes)

    if not shape_matches:
        return [ClassificationResult(
            path=source_path,
            shape=None,
            shape_confidence=0.0,
            classification=None,
            identification_confidence=0.0,
            id_protocol=None,
            reasoning="Source did not match any known shape.",
            needs_user_input=True,
        )]

    best_shape_match = shape_matches[0]

    # Container shapes: decompose into sub-units
    if best_shape_match.shape.is_container:
        groups = find_logical_groups(source_path, summary)
        if groups:
            results = []
            for group_path in groups:
                sub_results = classify_source(group_path, shapes, id_protocols, adapter, threshold)
                results.extend(sub_results)
            return results
        # Fallback: treat as a single unit if decomposition yields nothing
        return [ClassificationResult(
            path=source_path,
            shape=best_shape_match.shape,
            shape_confidence=best_shape_match.confidence,
            classification=None,
            identification_confidence=0.0,
            id_protocol=None,
            reasoning=f"Container shape '{best_shape_match.shape.name}' matched but could not be decomposed.",
            needs_user_input=True,
        )]

    # Leaf shape: run identification
    return [run_identification(
        source_path, summary, best_shape_match, id_protocols, adapter, threshold
    )]


# ---------------------------------------------------------------------------
# Mixed source decomposition
# ---------------------------------------------------------------------------

def find_logical_groups(path: Path, summary: StructuralSummary | None = None) -> list[Path]:
    """Decompose a mixed/container directory into logical sub-units.

    Clusters by: immediate subdirectories, extension family groups within flat
    directories, and date-pattern clusters.

    Returns a list of paths — each should be independently classifiable.
    """
    if not path.is_dir():
        return [path]

    if summary is None:
        summary = analyze_structure(path)

    # Strategy 1: if subdirectories exist, treat each top-level subdir as a unit
    top_level_dirs = [d for d in sorted(path.iterdir()) if d.is_dir() and d.name not in _SKIP_NAMES]
    top_level_files = [f for f in sorted(path.iterdir())
                       if f.is_file() and f.name not in _SKIP_NAMES
                       and not any(f.name.startswith(p) for p in _SKIP_PREFIXES)]

    if top_level_dirs:
        groups: list[Path] = list(top_level_dirs)
        # Add any loose files as a group if they exist
        if top_level_files:
            # Cluster loose files by extension family
            family_files: dict[str, list[Path]] = {}
            for f in top_level_files:
                fam = _file_family(f)
                family_files.setdefault(fam, []).append(f)
            # Only add as separate groups if meaningful
            for fam, files in family_files.items():
                if len(files) >= 2:
                    # Can't pass a virtual group — return path itself as fallback
                    groups.append(path)
                    break
        return groups

    # Strategy 2: flat directory — cluster by extension family
    if top_level_files:
        family_files_2: dict[str, list[Path]] = {}
        for f in top_level_files:
            fam = _file_family(f)
            family_files_2.setdefault(fam, []).append(f)

        if len(family_files_2) > 1:
            # Multiple families present — mixed directory, return path as-is
            # (let the learning flow handle it)
            return [path]

    return [path]


def _file_family(path: Path) -> str:
    ext = path.suffix.lower()
    for family, members in _EXTENSION_FAMILIES.items():
        if ext in members:
            return family
    return "other"
