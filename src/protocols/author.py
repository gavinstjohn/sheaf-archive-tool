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
from .loader import (
    load_all_protocols,
    load_identification_protocols,
    load_shapes,
    save_protocol,
    save_shape,
    validate_protocol_yaml,
    validate_shape_yaml,
)
from .model import IdentificationProtocol, ImportProtocol, protocol_from_dict, shape_from_dict
from .tools_registry import (
    add_tools,
    format_registry_for_prompt,
    load_tools,
)

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

_MAX_LIST_FILES = 50
_MAX_EXIF_FILES = 3


# ---------------------------------------------------------------------------
# Enrichment protocol authoring
# ---------------------------------------------------------------------------

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

    tools = load_tools(settings.tools_registry_path)
    registry_summary = format_registry_for_prompt(tools)
    system_prompt = _build_enrichment_system_prompt(registry_summary)

    session = ChatSession(adapter, system=system_prompt, max_tokens=8096)
    saved: list[EnrichmentProtocol] = []

    _register_enrichment_tools(session, imported_files, settings, saved)

    total = total_imported or len(imported_files)
    sample_names = ", ".join(p.name for p in imported_files[:5])
    opening = session.tool_loop(
        f"I just imported {total} file(s). Sample filenames: {sample_names}\n\n"
        "Please help me set up enrichment protocols for them. "
        "Use list_sample_files and read_file_metadata to understand the media, "
        "then suggest what enrichment steps would be valuable."
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

    # 3. List existing protocols
    session.register_tool(
        ToolDefinition(
            name="list_existing_protocols",
            description="List all existing enrichment (and import) protocols.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_protocols(settings),
    )

    # 4. Build tooling via Claude Code SDK agent
    session.register_tool(
        ToolDefinition(
            name="build_protocol_tooling",
            description=(
                "Spawn a Claude Code agent to install, configure, and verify whatever "
                "external tooling this enrichment protocol needs — any model, CLI tool, "
                "Python package, or custom script. Use this when the required tooling is "
                "not already in the tool registry. Always ask the user for confirmation "
                "before calling this tool."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Clear description of what needs to be installed/built and "
                            "what the resulting command should do."
                        ),
                    },
                    "media_context": {
                        "type": "string",
                        "description": (
                            "File type, expected input format, and the JSON output fields "
                            "the command_template should produce."
                        ),
                    },
                },
                "required": ["task", "media_context"],
            },
        ),
        lambda args: _tool_build_protocol_tooling(settings, args),
    )

    # 5. Save enrichment protocol (with auto-verification)
    session.register_tool(
        ToolDefinition(
            name="save_enrichment_protocol",
            description=(
                "Save a completed enrichment protocol. Call once the user has confirmed "
                "the protocol definition. Automatically runs a verification test on a "
                "sample file and returns the result."
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
        lambda args: _tool_save_enrichment_protocol(settings, imported_files, saved, args),
    )

    # 6. Finish the enrichment setup session
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


def _tool_build_protocol_tooling(settings: "Settings", args: dict) -> str:
    """Invoke the Claude Code SDK builder agent to set up tooling."""
    from .sdk_builder import run_sdk_builder

    task = args.get("task", "")
    media_context = args.get("media_context", "")
    if not task:
        return "Error: 'task' is required."

    tools = load_tools(settings.tools_registry_path)
    registry_summary = format_registry_for_prompt(tools)
    project_dir = settings.tools_registry_path.parent.parent  # config/.. = project root

    try:
        result = run_sdk_builder(task, media_context, registry_summary, project_dir)
    except Exception as e:
        return f"SDK agent failed: {e}"

    # Register new tools
    new_tools = result.get("new_tools", [])
    if new_tools:
        # We don't have a protocol name yet, use a placeholder
        add_tools(settings.tools_registry_path, new_tools, installed_by="(pending protocol)")

    # Return the command_template and notes to the authoring session
    lines = [f"command_template: {result['command_template']}"]
    if result.get("verification_output"):
        lines.append(f"\nVerification: {result['verification_output']}")
    if result.get("notes"):
        lines.append(f"\nNotes: {result['notes']}")
    if new_tools:
        lines.append(f"\nRegistered {len(new_tools)} new tool(s) in the tool registry.")

    return "\n".join(lines)


def _tool_save_enrichment_protocol(
    settings: "Settings",
    imported_files: list[Path],
    saved: list,
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

        # Update tool registry: fix "pending protocol" entries to use real name
        _update_pending_protocol_name(settings, protocol.name)

        # Auto-verify: run enrichment on first available file
        verification = _verify_enrichment_protocol(protocol, imported_files, settings)
        result = f"Protocol '{protocol.name}' saved to {path}."
        if verification:
            result += f"\n\nVerification result (run on sample file):\n{verification}"
        return result

    except Exception as e:
        return f"Error saving protocol: {e}"


def _update_pending_protocol_name(settings: "Settings", protocol_name: str) -> None:
    """Replace '(pending protocol)' in tools.yaml with the actual protocol name."""
    from .tools_registry import load_tools, save_tools
    tools = load_tools(settings.tools_registry_path)
    changed = False
    for t in tools:
        if t.get("installed_by") == "(pending protocol)":
            t["installed_by"] = protocol_name
            changed = True
    if changed:
        save_tools(settings.tools_registry_path, tools)


def _verify_enrichment_protocol(
    protocol,
    imported_files: list[Path],
    settings: "Settings",
) -> str:
    """Run the protocol on the first available file and return the output as a string."""
    # Find a candidate file
    candidate = next((p for p in imported_files if p.exists()), None)
    if candidate is None:
        return "(no sample file available for verification)"

    # Build a minimal sidecar read
    from ..jobs.worker import _sidecar_path_for
    rel = str(candidate.relative_to(settings.archive_root))
    sidecar_path = _sidecar_path_for(settings.archive_root, rel)

    try:
        from ..archive.sidecar import read_sidecar
        sidecar_data = read_sidecar(sidecar_path) if sidecar_path.exists() else {}
    except Exception:
        sidecar_data = {}

    # Build enrichment context and run
    import sqlite3
    from .executor import EnrichmentContext, run_enrichment

    # We use an in-memory db just for the verification run (don't index)
    conn = sqlite3.connect(":memory:")
    try:
        from ..db.schema import create_tables
        create_tables(conn)
    except Exception:
        pass

    ctx = EnrichmentContext(
        file_path=candidate,
        sidecar_path=sidecar_path,
        sidecar_data=sidecar_data,
        protocol=protocol,
        settings=settings,
        conn=conn,
    )

    try:
        from .executor import _run_command_enrichment, _run_claude_enrichment
        method = protocol.method or "command"
        if method == "command":
            result = _run_command_enrichment(ctx)
        elif method == "claude":
            # Don't run Claude enrichment in verification — too expensive
            return "(claude enrichment: skipping auto-verification)"
        else:
            return f"(unknown method {method!r}: skipping auto-verification)"

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return f"Verification failed: {e}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Import protocol authoring
# ---------------------------------------------------------------------------

def draft_import_protocol(
    source_path: Path,
    adapter: BaseAdapter,
    settings: "Settings",
    classification_ctx: dict | None = None,
) -> ImportProtocol | None:
    """Run the conversational import learning flow.

    Guides the user and the model through investigating the source,
    drafting a protocol (and optionally shape + identification protocol), and saving it.

    classification_ctx: optional dict with keys 'classification', 'shape', 'id_confidence'
    if the source has already been identified by the classifier pipeline.

    Returns the saved ImportProtocol, or None if the user aborted.
    """
    system_prompt = _load_system_prompt(settings)
    session = ChatSession(adapter, system=system_prompt, max_tokens=8096)
    saved_protocol: list[ImportProtocol] = []

    _register_tools(session, source_path, settings, saved_protocol)

    # Build opening context
    context_lines = [f"I want to import media from: {source_path}"]

    if classification_ctx:
        cls = classification_ctx.get("classification")
        shape = classification_ctx.get("shape")
        conf = classification_ctx.get("id_confidence", 0.0)
        if cls:
            context_lines.append(f"\nThe source has been identified as: {cls!r} (confidence {conf:.0%})")
        if shape:
            context_lines.append(f"Matched structural shape: {shape.name} — {shape.description}")
        context_lines.append(
            "\nSince identification already ran, you may skip to drafting the import protocol "
            "using `accepts_classification` (new-style) rather than `triggers`."
        )
    else:
        # Check if any shapes/identification protocols exist at all
        existing_shapes = load_shapes(settings.shapes_dir)
        if not existing_shapes:
            context_lines.append(
                "\nNo structural shapes have been defined yet. You may need to define a shape "
                "and identification protocol before (or alongside) the import protocol. "
                "Use save_shape and save_identification_protocol tools for this."
            )

    context_lines.append(
        "\n\nPlease investigate the source and help me create an import protocol. "
        "Start by examining the files and directory structure."
    )

    opening = session.tool_loop("\n".join(context_lines))
    print(f"\nSheaf: {opening}\n")
    readline_chat(session)

    if saved_protocol:
        return saved_protocol[0]
    return None


# ---------------------------------------------------------------------------
# Identification protocol authoring (standalone)
# ---------------------------------------------------------------------------

def draft_identification_protocol(
    adapter: BaseAdapter,
    settings: "Settings",
) -> None:
    """Standalone conversational session to create an identification protocol."""
    shapes = load_shapes(settings.shapes_dir)
    shapes_summary = _format_shapes_summary(shapes)

    system_prompt = _load_prompt("identification", shapes_summary=shapes_summary)
    session = ChatSession(adapter, system=system_prompt, max_tokens=8096)
    saved: list = []

    _register_identification_tools(session, settings, saved)

    opening = session.tool_loop(
        "I want to create a new identification protocol. "
        "Please walk me through defining what semantic classification it produces "
        "and which structural shapes trigger it."
    )
    print(f"\nSheaf: {opening}\n")
    readline_chat(session)

    if saved:
        print(f"\nSaved {len(saved)} identification protocol(s).")


def _register_identification_tools(
    session: ChatSession,
    settings: "Settings",
    saved: list,
) -> None:
    session.register_tool(
        ToolDefinition(
            name="list_existing_shapes",
            description="List all known structural shapes.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_shapes(settings),
    )
    session.register_tool(
        ToolDefinition(
            name="save_shape",
            description="Save a new structural shape definition.",
            input_schema={
                "type": "object",
                "properties": {
                    "shape_yaml": {
                        "type": "string",
                        "description": "Shape definition in YAML format.",
                    },
                },
                "required": ["shape_yaml"],
            },
        ),
        lambda args: _tool_save_shape(settings, args),
    )
    session.register_tool(
        ToolDefinition(
            name="save_identification_protocol",
            description="Save a completed identification protocol.",
            input_schema={
                "type": "object",
                "properties": {
                    "protocol_yaml": {
                        "type": "string",
                        "description": "Identification protocol in YAML format.",
                    },
                },
                "required": ["protocol_yaml"],
            },
        ),
        lambda args: _tool_save_identification_protocol(settings, saved, session, args),
    )


# ---------------------------------------------------------------------------
# Protocol editing
# ---------------------------------------------------------------------------

def edit_protocol(
    protocol_name: str,
    adapter: BaseAdapter,
    settings: "Settings",
) -> None:
    """Re-enter a conversational session to revise an existing protocol.

    The model is seeded with the current YAML and asks the user what they
    want changed. Saves the updated protocol once the user confirms.
    """
    from .loader import get_protocol
    from .model import EnrichmentProtocol

    try:
        protocol = get_protocol(protocol_name, settings.protocols_dir)
    except Exception as e:
        print(f"Error loading protocol: {e}")
        return

    # Serialize to YAML so the model can see it
    from .model import protocol_to_dict
    current_yaml = yaml.dump(protocol_to_dict(protocol), default_flow_style=False,
                             sort_keys=False, allow_unicode=True)

    tools = load_tools(settings.tools_registry_path)
    registry_summary = format_registry_for_prompt(tools)

    system_prompt = _load_prompt("edit", registry_summary=registry_summary)
    session = ChatSession(adapter, system=system_prompt, max_tokens=8096)

    # Register tools appropriate to the protocol type
    if isinstance(protocol, ImportProtocol):
        saved: list = []
        # For import editing, we need a source_path — use a dummy that clearly has none
        # The edit tools don't need to inspect source files, just validate/save
        _register_edit_import_tools(session, settings, saved)
        opening = session.tool_loop(
            f"I want to edit this import protocol:\n\n```yaml\n{current_yaml}```\n\n"
            "What would you like to change?"
        )
    else:
        saved = []
        imported_files: list[Path] = []  # no files context during standalone edit
        _register_edit_enrichment_tools(session, settings, saved)
        opening = session.tool_loop(
            f"I want to edit this enrichment protocol:\n\n```yaml\n{current_yaml}```\n\n"
            "What would you like to change?"
        )

    print(f"\nSheaf: {opening}\n")
    readline_chat(session)


def _register_edit_import_tools(
    session: ChatSession,
    settings: "Settings",
    saved: list,
) -> None:
    """Tools for editing an existing import protocol (no source to inspect)."""
    session.register_tool(
        ToolDefinition(
            name="list_existing_protocols",
            description="List all existing protocols for reference.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_protocols(settings),
    )
    session.register_tool(
        ToolDefinition(
            name="save_protocol",
            description=(
                "Save the revised import protocol, overwriting the previous version. "
                "Call once the user has confirmed the changes are correct."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "protocol_yaml": {"type": "string"},
                },
                "required": ["protocol_yaml"],
            },
        ),
        lambda args: _tool_save_protocol(settings, saved, session, args),
    )


def _register_edit_enrichment_tools(
    session: ChatSession,
    settings: "Settings",
    saved: list,
) -> None:
    """Tools for editing an existing enrichment protocol."""
    session.register_tool(
        ToolDefinition(
            name="list_existing_protocols",
            description="List all existing protocols for reference.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_protocols(settings),
    )
    session.register_tool(
        ToolDefinition(
            name="build_protocol_tooling",
            description=(
                "Spawn a Claude Code agent to install or update tooling this protocol needs. "
                "Ask the user before calling this."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "media_context": {"type": "string"},
                },
                "required": ["task", "media_context"],
            },
        ),
        lambda args: _tool_build_protocol_tooling(settings, args),
    )
    session.register_tool(
        ToolDefinition(
            name="save_enrichment_protocol",
            description=(
                "Save the revised enrichment protocol, overwriting the previous version. "
                "Call once the user has confirmed the changes are correct."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "protocol_yaml": {"type": "string"},
                },
                "required": ["protocol_yaml"],
            },
        ),
        # No files to verify against during standalone edit — skip verification
        lambda args: _tool_save_enrichment_protocol(settings, [], saved, args),
    )
    session.register_tool(
        ToolDefinition(
            name="finish_enrichment_setup",
            description="Call when editing is complete.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_finish_enrichment(session, saved),
    )


# ---------------------------------------------------------------------------
# Protocol explanation
# ---------------------------------------------------------------------------

def explain_protocol(
    protocol_name: str,
    adapter: BaseAdapter,
    settings: "Settings",
) -> None:
    """Conversational explanation of what a protocol does and why.

    More natural-language than `sheaf protocols show`.
    """
    from .loader import get_protocol
    from .model import protocol_to_dict

    try:
        protocol = get_protocol(protocol_name, settings.protocols_dir)
    except Exception as e:
        print(f"Error: {e}")
        return

    current_yaml = yaml.dump(protocol_to_dict(protocol), default_flow_style=False,
                             sort_keys=False, allow_unicode=True)

    system_prompt = (
        "You are a helpful assistant explaining how a Sheaf media archive protocol works. "
        "Given a protocol definition, explain in plain language: what files it handles, "
        "what it does with them, where they end up, and what enrichment runs. "
        "Be concise but complete. After your explanation, invite the user to ask questions."
    )
    session = ChatSession(adapter, system=system_prompt, max_tokens=4096)
    # Read-only: register list_existing_protocols only
    session.register_tool(
        ToolDefinition(
            name="list_existing_protocols",
            description="List all protocols for context.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_protocols(settings),
    )

    opening = session.tool_loop(
        f"Please explain this protocol:\n\n```yaml\n{current_yaml}```"
    )
    print(f"\nSheaf: {opening}\n")
    readline_chat(session)


# ---------------------------------------------------------------------------
# Tool registrations (import authoring)
# ---------------------------------------------------------------------------

def _register_tools(
    session: ChatSession,
    source_path: Path,
    settings: "Settings",
    saved_protocol: list,
) -> None:
    # Shape and identification tools (for new sources that need new layers)
    session.register_tool(
        ToolDefinition(
            name="list_existing_shapes",
            description="List all known structural shapes.",
            input_schema={"type": "object", "properties": {}},
        ),
        lambda _: _tool_list_shapes(settings),
    )
    session.register_tool(
        ToolDefinition(
            name="save_shape",
            description=(
                "Save a structural shape definition. Use when this source type is "
                "new and no existing shape describes its structure."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "shape_yaml": {
                        "type": "string",
                        "description": "Shape definition in YAML format.",
                    },
                },
                "required": ["shape_yaml"],
            },
        ),
        lambda args: _tool_save_shape(settings, args),
    )
    session.register_tool(
        ToolDefinition(
            name="save_identification_protocol",
            description=(
                "Save an identification protocol that classifies this source type. "
                "Use when there is no existing identification protocol for the shape."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "protocol_yaml": {
                        "type": "string",
                        "description": "Identification protocol in YAML format.",
                    },
                },
                "required": ["protocol_yaml"],
            },
        ),
        lambda args: _tool_save_identification_protocol(settings, [], None, args),
    )

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

def _tool_list_shapes(settings: "Settings") -> str:
    shapes = load_shapes(settings.shapes_dir)
    if not shapes:
        return "No shapes defined yet."
    lines = ["Known shapes:"]
    for name, s in sorted(shapes.items()):
        lines.append(f"\n[shape] {name}")
        lines.append(f"  Description: {s.description}")
        lines.append(f"  Container: {s.is_container}")
        for ind in s.indicators:
            for k, v in ind.items():
                lines.append(f"  indicator: {k}: {v}")
    return "\n".join(lines)


def _tool_save_shape(settings: "Settings", args: dict) -> str:
    import yaml as _yaml
    shape_yaml = args.get("shape_yaml", "")
    try:
        data = _yaml.safe_load(shape_yaml)
        errors = validate_shape_yaml(data)
        if errors:
            return "Cannot save — validation errors:\n" + "\n".join(f"  - {e}" for e in errors)
        shape = shape_from_dict(data)
        path = save_shape(shape, settings.shapes_dir)
        return f"Shape '{shape.name}' saved to {path}."
    except Exception as e:
        return f"Error saving shape: {e}"


def _tool_save_identification_protocol(
    settings: "Settings",
    saved: list,
    session: "ChatSession | None",
    args: dict,
) -> str:
    import yaml as _yaml
    protocol_yaml = args.get("protocol_yaml", "")
    try:
        data = _yaml.safe_load(protocol_yaml)
        data["maturity"] = "draft"
        errors = validate_protocol_yaml(data)
        if errors:
            return "Cannot save — validation errors:\n" + "\n".join(f"  - {e}" for e in errors)
        protocol = protocol_from_dict(data)
        if not isinstance(protocol, IdentificationProtocol):
            return "Error: this is not an identification protocol (type must be 'identification')."
        path = save_protocol(protocol, settings.protocols_dir)
        saved.append(protocol)
        if session is not None:
            session.done = True
        return f"Identification protocol '{protocol.name}' saved to {path}."
    except Exception as e:
        return f"Error saving protocol: {e}"


def _format_shapes_summary(shapes: dict) -> str:
    if not shapes:
        return "No shapes defined yet."
    lines = []
    for name, s in sorted(shapes.items()):
        lines.append(f"  {name}: {s.description}")
    return "\n".join(lines)


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
    id_protocols = load_identification_protocols(settings.protocols_dir)

    if not imports and not enrichments and not id_protocols:
        return "No protocols exist yet."

    lines = ["Existing protocols:"]

    for name, p in sorted(id_protocols.items()):
        lines.append(f"\n[identification] {name} ({p.maturity.value})")
        lines.append(f"  Description: {p.description}")
        lines.append(f"  Classification: {p.classification}")
        lines.append(f"  Method: {p.method}")
        shapes_triggered = [t.get("shape", "") for t in p.triggers]
        if shapes_triggered:
            lines.append(f"  Triggered by shapes: {', '.join(shapes_triggered)}")

    for name, p in sorted(imports.items()):
        lines.append(f"\n[import] {name} ({p.maturity.value})")
        lines.append(f"  Description: {p.description}")
        if p.accepts_classification:
            lines.append(f"  Accepts classification: {p.accepts_classification}")
        elif p.triggers:
            lines.append(f"  Triggers (legacy): {p.triggers}")
        lines.append(f"  Category: {p.category_template}" +
                     (f" / {p.subcategory_template}" if p.subcategory_template else ""))
        lines.append(f"  Filename: {p.filename_template}")

    for name, p in sorted(enrichments.items()):
        lines.append(f"\n[enrichment] {name} ({p.maturity.value})")
        lines.append(f"  Description: {p.description}")
        if p.command_template:
            lines.append(f"  Command: {p.command_template[:80]}")

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
        result = executor.plan(source_path, protocol, settings)
        return executor.preview(result, settings.archive_root)

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
# System prompts
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts"


def _load_prompt(name: str, **kwargs) -> str:
    """Load a prompt from config/prompts/<name>.txt, applying any format kwargs."""
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    text = path.read_text()
    return text.format(**kwargs) if kwargs else text


def _build_enrichment_system_prompt(registry_summary: str) -> str:
    return _load_prompt("enrichment", registry_summary=registry_summary)


def _load_system_prompt(settings: "Settings") -> str:
    return _load_prompt("import")
