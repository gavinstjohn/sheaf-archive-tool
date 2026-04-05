from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ..exceptions import ProtocolNotFoundError, ProtocolValidationError
from .model import (
    EnrichmentProtocol,
    ImportProtocol,
    protocol_from_dict,
    protocol_to_dict,
)

log = logging.getLogger(__name__)

REQUIRED_FIELDS = {"name", "type", "version", "created", "maturity", "description"}
VALID_TYPES = {"import", "enrichment"}
VALID_MATURITIES = {"draft", "probationary", "trusted"}


def validate_protocol_yaml(data: dict) -> list[str]:
    """Return a list of validation error strings; empty list means valid."""
    errors = []

    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"Missing required fields: {', '.join(sorted(missing))}")

    if data.get("type") not in VALID_TYPES:
        errors.append(f"Invalid type: {data.get('type')!r} — must be one of {VALID_TYPES}")

    if data.get("maturity") not in VALID_MATURITIES:
        errors.append(f"Invalid maturity: {data.get('maturity')!r} — must be one of {VALID_MATURITIES}")

    if data.get("type") == "import":
        if not data.get("category_template"):
            errors.append("Import protocol missing 'category_template'")
        if not data.get("filename_template"):
            errors.append("Import protocol missing 'filename_template'")

    return errors


def load_protocol_file(path: Path) -> ImportProtocol | EnrichmentProtocol:
    """Load and validate a single protocol YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ProtocolValidationError(f"{path}: file is empty or not a YAML mapping")

    errors = validate_protocol_yaml(data)
    if errors:
        raise ProtocolValidationError(
            f"{path}: validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    try:
        return protocol_from_dict(data)
    except (KeyError, ValueError) as e:
        raise ProtocolValidationError(f"{path}: {e}") from e


def load_import_protocols(protocols_dir: Path) -> dict[str, ImportProtocol]:
    """Load all import protocols from protocols/import/*.yaml."""
    result: dict[str, ImportProtocol] = {}
    import_dir = protocols_dir / "import"
    if not import_dir.exists():
        return result
    for path in sorted(import_dir.glob("*.yaml")):
        try:
            p = load_protocol_file(path)
            if isinstance(p, ImportProtocol):
                result[p.name] = p
            else:
                log.warning("Expected import protocol in %s, got type %r — skipping", path, p.type)
        except ProtocolValidationError as e:
            log.warning("Skipping %s: %s", path, e)
    return result


def load_enrichment_protocols(protocols_dir: Path) -> dict[str, EnrichmentProtocol]:
    """Load all enrichment protocols from protocols/enrichment/*.yaml."""
    result: dict[str, EnrichmentProtocol] = {}
    enrich_dir = protocols_dir / "enrichment"
    if not enrich_dir.exists():
        return result
    for path in sorted(enrich_dir.glob("*.yaml")):
        try:
            p = load_protocol_file(path)
            if isinstance(p, EnrichmentProtocol):
                result[p.name] = p
            else:
                log.warning("Expected enrichment protocol in %s, got type %r — skipping", path, p.type)
        except ProtocolValidationError as e:
            log.warning("Skipping %s: %s", path, e)
    return result


def load_all_protocols(
    protocols_dir: Path,
) -> tuple[dict[str, ImportProtocol], dict[str, EnrichmentProtocol]]:
    return (
        load_import_protocols(protocols_dir),
        load_enrichment_protocols(protocols_dir),
    )


def save_protocol(
    protocol: ImportProtocol | EnrichmentProtocol,
    protocols_dir: Path,
) -> Path:
    """Serialize a protocol to YAML and save it. Returns the path written."""
    subdir = protocols_dir / protocol.type
    subdir.mkdir(parents=True, exist_ok=True)
    path = subdir / f"{protocol.name}.yaml"
    data = protocol_to_dict(protocol)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    log.info("Saved protocol %r to %s", protocol.name, path)
    return path


def get_protocol(
    name: str,
    protocols_dir: Path,
) -> ImportProtocol | EnrichmentProtocol:
    """Load a single protocol by name, searching both import/ and enrichment/."""
    for subdir in ("import", "enrichment"):
        path = protocols_dir / subdir / f"{name}.yaml"
        if path.exists():
            return load_protocol_file(path)
    raise ProtocolNotFoundError(f"Protocol not found: {name!r}")
