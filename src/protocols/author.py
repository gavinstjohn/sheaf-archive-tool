from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..adapter.base import BaseAdapter, ToolDefinition
from ..chat.session import ChatSession, readline_chat
from ..exceptions import ProtocolValidationError
from .loader import load_all_protocols, save_protocol, validate_protocol_yaml
from .model import ImportProtocol, protocol_from_dict

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

_MAX_LIST_FILES = 50
_MAX_EXIF_FILES = 3


def draft_enrichment_protocol(
    imported_files: list[Path],
    adapter: BaseAdapter,
    settings: "Settings",
    total_imported: int | None = None,
) -> None:
    """Run the conversational enrichment learning flow.

    Guides the user and the model through defining what enrichment steps
    should run on recently imported files, then saves the protocol(s).

    imported_files: absolute paths to newly imported media files (sample).
    Returns nothing — protocols are saved directly.
    """
    from .model import EnrichmentProtocol

    system_prompt = _ENRICHMENT_SYSTEM_PROMPT
    session = ChatSession(adapter, system=system_prompt, max_tokens=8096)
    saved: list[EnrichmentProtocol] = []

    _register_enrichment_tools(session, imported_files, settings, saved)

    total = total_imported or len(imported_files)
    sample_names = ", ".join(p.name for p in imported_files[:5])
    opening = session.tool_loop(
        f"I just imported {total} file(s). Sample filenames: {sample_names}\n\n"
        "Please help me set up enrichment protocols for them. "
        "Use list_sample_files and read_file_metadata to understand the media, "
        "then suggest what enrichment steps would be valuable. "
        "Prefer local execution methods (command or ollama) over the Claude API."
    )

    print(f"\nSheaf: {opening}\n")
    readline_chat(session)

    if saved:
        print(f"\nSaved {len(saved)} enrichment protocol(s):")
        for p in saved:
            print(f"  {p.name}")


def _register_enrichment_tools(
    session: ChatSession,
    imported_files: list[Path],
    settings: "Settings",
    saved: list,
) -> None:
    from ..adapter.base import ToolDefinition

    # 1. List sample imported files
    session.register_tool(
        ToolDefinition(
            name="list_sample_files",
            description="List the recently imported files available for inspection.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_sample_files(imported_files),
    )

    # 2. Read EXIF / sidecar for a sample file
    session.register_tool(
        ToolDefinition(
            name="read_file_metadata",
            description=(
                "Read the sidecar metadata and EXIF data for one of the imported files. "
                "Use this to understand what metadata is available for enrichment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename (not full path) of one of the imported files.",
                    },
                },
                "required": ["filename"],
            },
        ),
        lambda args: _tool_read_file_metadata(imported_files, settings, args),
    )

    # 3. List existing enrichment protocols
    session.register_tool(
        ToolDefinition(
            name="list_existing_protocols",
            description="List all existing enrichment (and import) protocols.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_protocols(settings),
    )

    # 4. Save enrichment protocol
    session.register_tool(
        ToolDefinition(
            name="save_enrichment_protocol",
            description=(
                "Save a completed enrichment protocol. Call once the user has confirmed "
                "the protocol definition."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "protocol_yaml": {
                        "type": "string",
                        "description": "The complete enrichment protocol in YAML format.",
                    },
                },
                "required": ["protocol_yaml"],
            },
        ),
        lambda args: _tool_save_enrichment_protocol(settings, saved, session, args),
    )

    # 5. Finish the enrichment setup session
    session.register_tool(
        ToolDefinition(
            name="finish_enrichment_setup",
            description=(
                "Call this when the enrichment setup is complete and the user is done. "
                "Use this after saving all desired protocols, or if the user declines enrichment."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_finish_enrichment(session, saved),
    )


def _tool_list_sample_files(files: list[Path]) -> str:
    if not files:
        return "No sample files available."
    lines = [f"  {p.name}  ({p.suffix.lower()}, {p.stat().st_size:,} bytes)"
             for p in files if p.exists()]
    return f"{len(lines)} imported file(s):\n" + "\n".join(lines)


def _tool_read_file_metadata(
    imported_files: list[Path],
    settings: "Settings",
    args: dict,
) -> str:
    filename = args.get("filename", "")
    match = next((p for p in imported_files if p.name == filename), None)
    if match is None:
        return f"File not found in sample: {filename}"

    lines = [f"File: {match}"]

    # Sidecar
    from ..jobs.worker import _sidecar_path_for
    rel = str(match.relative_to(settings.archive_root))
    sidecar = _sidecar_path_for(settings.archive_root, rel)
    if sidecar.exists():
        from ..archive.sidecar import read_sidecar
        lines.append("Sidecar:")
        lines.append(json.dumps(read_sidecar(sidecar), indent=2, default=str))
    else:
        lines.append("(no sidecar found)")

    # EXIF
    result = _tool_read_exif(match.parent, {"file_path": match.name})
    lines.append("EXIF/metadata:")
    lines.append(result)

    return "\n".join(lines)


def _tool_finish_enrichment(session: ChatSession, saved: list) -> str:
    session.done = True
    if saved:
        names = ", ".join(p.name for p in saved)
        return f"Enrichment setup complete. Saved: {names}."
    return "Enrichment setup skipped."


def _tool_save_enrichment_protocol(
    settings: "Settings",
    saved: list,
    session: ChatSession,
    args: dict,
) -> str:
    protocol_yaml = args.get("protocol_yaml", "")
    try:
        data = yaml.safe_load(protocol_yaml)
        data["maturity"] = "draft"
        errors = validate_protocol_yaml(data)
        if errors:
            return "Cannot save — validation errors:\n" + "\n".join(f"  - {e}" for e in errors)

        from .model import EnrichmentProtocol
        protocol = protocol_from_dict(data)
        if not isinstance(protocol, EnrichmentProtocol):
            return "Error: this is not an enrichment protocol (type must be 'enrichment')."

        path = save_protocol(protocol, settings.protocols_dir)
        saved.append(protocol)
        # Don't set session.done — user might want to save more than one protocol
        return f"Protocol '{protocol.name}' saved to {path}."

    except Exception as e:
        return f"Error saving protocol: {e}"


_ENRICHMENT_SYSTEM_PROMPT = """\
You are the enrichment assistant for Sheaf, a personal media archive management system.

Your job is to help the user define enrichment protocols — background processing steps
that extract metadata, generate descriptions, create tags, run OCR, etc.

## Execution methods (choose the right one)

Sheaf supports three execution methods. **Default to local methods** — they are free,
private, and work offline. Only suggest `method: claude` when the user explicitly asks.

### method: command (preferred for scripts and CLI tools)
Runs a shell command. The command must print JSON to stdout.
Variables available in command_template: {file_path}, {archive_root}, {sidecar_path}

Example — using exiftool to extract GPS:
```yaml
method: command
command_template: exiftool -json -GPS:all "{file_path}" | python3 -c "import sys,json; d=json.load(sys.stdin)[0]; print(json.dumps({'lat': d.get('GPSLatitude'), 'lon': d.get('GPSLongitude')}))"
```

Example — calling a custom Python script:
```yaml
method: command
command_template: python3 /path/to/describe.py "{file_path}"
```

### method: ollama (preferred for vision/language tasks)
Calls a locally running Ollama model. Ideal for image description, tagging, captioning.
Requires Ollama to be running (`ollama serve`) with the chosen model pulled.

```yaml
method: ollama
ollama_model: llava        # or llama3.2-vision, moondream, bakllava, etc.
instructions: |
  Describe what you see in this photo. Return JSON with:
  description (one sentence), tags (list of strings), subject (landscape/portrait/etc.)
```

### method: claude (only when user requests it)
Calls the Claude API. Costs money; requires ANTHROPIC_API_KEY; not private.
Use only if the user explicitly wants Claude-based enrichment.

```yaml
method: claude
instructions: |
  Analyze the file metadata and produce enrichment data.
```

## Full protocol format (YAML)

```yaml
name: <unique-identifier>
type: enrichment
version: "1"
created: "YYYY-MM-DD"
maturity: draft
description: <one sentence>
media_types: [photo]             # file_type categories this applies to
output_fields:                   # JSON keys this protocol produces
  - description
  - tags
method: command                  # or ollama, claude
command_template: "..."          # if method=command
ollama_model: "..."              # if method=ollama
instructions: |                  # prompt for ollama or claude; notes for command
  ...
```

## Your workflow

1. Use list_sample_files and read_file_metadata to understand the media.
2. Use list_existing_protocols to avoid duplicating existing enrichment.
3. Suggest enrichment steps; ask the user what matters to them.
4. Ask what local tools are available (exiftool, ollama, custom scripts).
5. Draft a protocol using the appropriate method; confirm with the user.
6. Call save_enrichment_protocol once confirmed.

## Important

- Always ask about local tooling before suggesting ollama or command methods.
- output_fields should be concrete and useful for search (e.g. description, tags, gps_coords).
- Protocol name: lowercase with hyphens (e.g. photo-description-ollama, photo-exif-gps).
- Multiple enrichment protocols can run on the same file type.
- The user can always skip enrichment setup now and add it later.
- When the user is done (all protocols saved, or they decline enrichment), call
  finish_enrichment_setup to end the session cleanly.
"""


def draft_import_protocol(
    source_path: Path,
    adapter: BaseAdapter,
    settings: "Settings",
) -> ImportProtocol | None:
    """Run the conversational import learning flow.

    Guides the user and the model through investigating the source,
    drafting a protocol, previewing it, and saving it.

    Returns the saved ImportProtocol, or None if the user aborted.
    """
    system_prompt = _load_system_prompt(settings)
    session = ChatSession(adapter, system=system_prompt, max_tokens=8096)
    saved_protocol: list[ImportProtocol] = []  # mutable container

    # Register tools
    _register_tools(session, source_path, settings, saved_protocol)

    # Seed the conversation with context about what we're doing
    opening = session.tool_loop(
        f"I want to import media from: {source_path}\n\n"
        "Please investigate the source and help me create an import protocol. "
        "Start by examining the files and directory structure, then ask me any "
        "questions you need to draft a protocol."
    )

    print(f"\nSheaf: {opening}\n")

    # Hand off to the readline loop
    readline_chat(session)

    if saved_protocol:
        return saved_protocol[0]
    return None


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------

def _register_tools(
    session: ChatSession,
    source_path: Path,
    settings: "Settings",
    saved_protocol: list,
) -> None:

    # 1. List source files
    session.register_tool(
        ToolDefinition(
            name="list_source_files",
            description=(
                "List files in the import source directory. "
                "Use this to understand the structure and file types present."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subdirectory": {
                        "type": "string",
                        "description": "Optional subdirectory relative to source root to list. Omit to list the root.",
                    },
                    "max_files": {
                        "type": "integer",
                        "description": f"Maximum files to return (default {_MAX_LIST_FILES}).",
                    },
                },
            },
        ),
        lambda args: _tool_list_source_files(source_path, args),
    )

    # 2. Read EXIF data
    session.register_tool(
        ToolDefinition(
            name="read_exif",
            description=(
                "Read EXIF metadata from a file in the source. "
                "Use this to understand what metadata is available (capture date, camera model, etc.)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file, relative to the source root.",
                    },
                },
                "required": ["file_path"],
            },
        ),
        lambda args: _tool_read_exif(source_path, args),
    )

    # 3. List existing protocols
    session.register_tool(
        ToolDefinition(
            name="list_existing_protocols",
            description=(
                "List all existing import and enrichment protocols. "
                "Use this to understand what conventions are already in use."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _args: _tool_list_protocols(settings),
    )

    # 4. Preview protocol
    session.register_tool(
        ToolDefinition(
            name="preview_protocol",
            description=(
                "Given a protocol in YAML format, show a dry-run preview of how files "
                "from the source would be imported. Use this to verify the protocol "
                "produces correct filenames and paths before saving."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "protocol_yaml": {
                        "type": "string",
                        "description": "The complete protocol definition in YAML format.",
                    },
                },
                "required": ["protocol_yaml"],
            },
        ),
        lambda args: _tool_preview_protocol(source_path, settings, args),
    )

    # 5. Save (finish) protocol
    session.register_tool(
        ToolDefinition(
            name="save_protocol",
            description=(
                "Save the completed import protocol. Call this once the user has confirmed "
                "the protocol is correct. The protocol will be saved as 'draft' maturity "
                "and used for the current import."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "protocol_yaml": {
                        "type": "string",
                        "description": "The final, confirmed protocol in YAML format.",
                    },
                },
                "required": ["protocol_yaml"],
            },
        ),
        lambda args: _tool_save_protocol(settings, saved_protocol, session, args),
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_list_source_files(source_path: Path, args: dict) -> str:
    subdir = args.get("subdirectory", "")
    max_files = int(args.get("max_files", _MAX_LIST_FILES))
    target = source_path / subdir if subdir else source_path

    if not target.exists():
        return f"Directory not found: {subdir}"

    lines = []
    count = 0
    ext_counts: dict[str, int] = {}

    for p in sorted(target.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith("._") or p.name == ".DS_Store":
            continue
        ext = p.suffix.lower() or "(no ext)"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        if count < max_files:
            rel = p.relative_to(source_path)
            lines.append(f"{rel}  ({p.stat().st_size:,} bytes)")
        count += 1

    result = "\n".join(lines)
    if count > max_files:
        result += f"\n... and {count - max_files} more files"
    result += f"\n\nTotal: {count} files"
    result += "\nExtensions: " + ", ".join(
        f"{e} ({n})" for e, n in sorted(ext_counts.items(), key=lambda x: -x[1])
    )
    return result


def _tool_read_exif(source_path: Path, args: dict) -> str:
    rel = args.get("file_path", "")
    target = source_path / rel
    if not target.exists():
        return f"File not found: {rel}"

    try:
        result = subprocess.run(
            ["exiftool", "-json", "-G", str(target)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data:
                # Return a readable subset of the most useful tags
                tags = data[0]
                useful = {k: v for k, v in tags.items()
                          if any(kw in k for kw in
                                 ["Date", "Time", "Camera", "Make", "Model",
                                  "Image", "File", "GPS", "Exposure", "Focal"])}
                return json.dumps(useful, indent=2, default=str)
        return f"exiftool returned: {result.stderr[:200]}"
    except FileNotFoundError:
        return "exiftool not available — cannot read EXIF data"
    except subprocess.TimeoutExpired:
        return "exiftool timed out"


def _tool_list_protocols(settings: "Settings") -> str:
    imports, enrichments = load_all_protocols(settings.protocols_dir)
    if not imports and not enrichments:
        return "No protocols exist yet."

    lines = ["Existing protocols:"]
    for name, p in imports.items():
        lines.append(f"\n[import] {name} ({p.maturity.value})")
        lines.append(f"  Description: {p.description}")
        lines.append(f"  Triggers: {p.triggers}")
        lines.append(f"  Category: {p.category_template}" +
                     (f" / {p.subcategory_template}" if p.subcategory_template else ""))
        lines.append(f"  Filename: {p.filename_template}")
    for name, p in enrichments.items():
        lines.append(f"\n[enrichment] {name} ({p.maturity.value})")
        lines.append(f"  Description: {p.description}")
    return "\n".join(lines)


def _tool_preview_protocol(source_path: Path, settings: "Settings", args: dict) -> str:
    protocol_yaml = args.get("protocol_yaml", "")
    try:
        data = yaml.safe_load(protocol_yaml)
        errors = validate_protocol_yaml(data)
        if errors:
            return "Validation errors:\n" + "\n".join(f"  - {e}" for e in errors)

        protocol = protocol_from_dict(data)
        if not isinstance(protocol, ImportProtocol):
            return "Error: this is not an import protocol."

        from .executor import ProtocolExecutor
        executor = ProtocolExecutor()
        actions = executor.plan(source_path, protocol, settings)
        return executor.preview(actions, settings.archive_root)

    except Exception as e:
        return f"Error previewing protocol: {e}"


def _tool_save_protocol(
    settings: "Settings",
    saved_protocol: list,
    session: ChatSession,
    args: dict,
) -> str:
    protocol_yaml = args.get("protocol_yaml", "")
    try:
        data = yaml.safe_load(protocol_yaml)
        # Force maturity to draft
        data["maturity"] = "draft"
        errors = validate_protocol_yaml(data)
        if errors:
            return "Cannot save — validation errors:\n" + "\n".join(f"  - {e}" for e in errors)

        protocol = protocol_from_dict(data)
        if not isinstance(protocol, ImportProtocol):
            return "Error: this is not an import protocol."

        path = save_protocol(protocol, settings.protocols_dir)
        saved_protocol.append(protocol)
        session.done = True
        return f"Protocol '{protocol.name}' saved to {path}. Ready to import."

    except Exception as e:
        return f"Error saving protocol: {e}"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _load_system_prompt(settings: "Settings") -> str:
    prompt_path = Path(__file__).parent.parent.parent / "config" / "system_prompt.txt"
    if prompt_path.exists():
        return prompt_path.read_text()
    return _DEFAULT_SYSTEM_PROMPT


_DEFAULT_SYSTEM_PROMPT = """You are the import assistant for Sheaf, a personal media archive management system.

Your job is to help the user create import protocols that define how to bring media from a specific source into the archive.

## Archive structure

The archive is organised as:
  <archive_root>/YYYY/YYYYMMDD/<category>/[<subcategory>/]/YYYYMMDD_<suffix>.<ext>

All metadata lives in a parallel .meta/ tree:
  <archive_root>/YYYY/YYYYMMDD/.meta/<category>/[<subcategory>/]/YYYYMMDD_<suffix>.<ext>.json

The framework enforces the YYYY/YYYYMMDD/ hierarchy and the YYYYMMDD_ filename prefix.
Everything below that — category directories, subcategories, filename suffixes — is defined by the import protocol.

## Protocol format (YAML)

```yaml
name: <unique-identifier>        # e.g. panasonic-dmc-ts3
type: import
version: "1"
created: "YYYY-MM-DD"
maturity: draft
description: <one sentence>
triggers:
  - extensions: [.jpg, .jpeg]    # file extensions this protocol handles
  # other trigger conditions as needed
category_template: "<category>"          # e.g. "photo", or a fixed string
subcategory_template: "<subcategory>"    # optional; omit or set to null if not needed
filename_template: "{date}_{time}_{original_name}"   # without extension
enrichment_chain: []             # list of enrichment protocol names to run after import
instructions: ""                 # optional notes for future reference
```

Template variables available: {date} (YYYYMMDD), {time} (HHMM), {original_name} (filename stem),
{original_filename} (full filename), {extension} (without dot), {index}, {index2}, {index4}.

## Your workflow

1. Use list_source_files to understand the source structure and file types.
2. Use read_exif on 1-2 representative files to see what metadata is available.
3. Use list_existing_protocols to see what conventions are already in use.
4. Ask the user any questions needed to determine: category, subcategory, filename format, which files to import.
5. Draft a protocol and use preview_protocol to verify it produces correct filenames and paths.
6. Show the preview to the user and ask for confirmation.
7. Once confirmed, call save_protocol with the final YAML.

## Important

- Be precise about filenames. Show concrete examples from the preview.
- Ask about files the user might want to skip (e.g. sidecar files, video alongside photos).
- If the source has multiple file types (photo + video), ask whether they should use one protocol or separate ones.
- Keep it simple — the user can always refine the protocol later.
- The protocol name should be descriptive and use lowercase with hyphens (e.g. panasonic-dmc-ts3).
"""
