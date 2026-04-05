# Sheaf — Design Requirements Document

## 1. Project Overview and Philosophy

Sheaf is a personal media archive management system built on top of an existing, human-readable filesystem archive. The archive stores creative output — of any media type — organized by date.

The tool adds intelligence to this archive without becoming a dependency. It provides three capabilities:

- **Import**: Ingest new media from arbitrary sources (SD cards, directories, files) into the archive's directory structure.
- **Enrichment**: Process stored media to extract metadata, generate transcriptions, create search embeddings, and perform other analysis that makes the archive more useful.
- **Access**: Search, filter, and browse the archive through both a CLI and a lightweight web GUI.

The tool is designed as a **self-assembling pipeline framework**. On day one, it knows how to place files into the archive's fixed directory structure, but it does not know how to handle any specific media type. Through conversational collaboration with the user, it learns how to import and enrich each type of media it encounters, codifying those decisions into reusable **protocols**. Over time, the system becomes increasingly autonomous while always deferring to the user when uncertainty arises.

### What this project is not

This is not a generic automation framework. It is an archive management tool with a specific domain, a fixed directory structure, and a defined database schema. The "learning" aspect is scoped to media handling — how to import from specific devices and how to enrich specific media types. The bones of the system (directory layout, metadata format, database, job queue, CLI interface) are hardcoded and stable.

---

## 2. Design Principles

### 2.1 Archive as Ground Truth

The filesystem archive is the canonical store of all media and metadata. The database is a derived, rebuildable index. If the database is deleted, a `reindex` command can reconstruct it entirely from the filesystem. If the tool is uninstalled, the archive remains a fully functional, human-navigable collection of dated media.

### 2.2 Append-Mostly Filesystem Safety

The archive is precious and the system treats it accordingly:

- Never overwrite an existing file without explicit user confirmation.
- Default to copy, not move. Source files are not deleted unless the user opts in.
- All destructive operations are staged as dry-run previews before execution.
- A structured operation log records every filesystem change the tool makes, with enough detail to roll back any operation. The log uses full snapshots for human-touched metadata and lightweight records for regenerable machine outputs.
- The frontier model actively verifies its own work against what was agreed upon with the user, especially when running new or probationary protocols.
- Git tracks the tool's project directory (source code, protocols, configuration, and operation logs) but never the archive itself. The archive remains completely tool-independent.

### 2.3 Local-First Processing

All media processing (OCR, transcription, vision analysis, embedding generation) runs locally using open-source models. Long processing times are acceptable — jobs run in the background and resurface to the user when complete or when input is needed. No cloud AI is required for enrichment.

### 2.4 Model-Agnostic Orchestration

The frontier model (used for decision-making, protocol authoring, and conversational interaction) is accessed through an adapter layer. The framework never communicates directly with any specific LLM API. Instructions and prompts are written in plain, provider-agnostic language. The initial implementation uses Claude, but the adapter can be replaced to support any frontier model.

### 2.5 Fixed Structure, Learned Strategy

The **structure** of the archive is fixed and encoded in the framework: directory hierarchy, filename prefix conventions, metadata format, database schema, job lifecycle. The **strategy** for handling specific media is learned through protocols: how to extract timestamps from a specific source's files, what enrichment steps apply to a given media type, how to parse a device's file layout. Protocols operate within the fixed structure — they determine what goes where, but the framework decides where "where" is.

### 2.6 Composable Protocols

Protocols are modular units of work organized into four distinct layers, each answering a different question:

- **Shape**: "What structural pattern does this source match?" Detected cheaply by the framework before any expensive processing.
- **Identification**: "What does this content represent semantically?" Can sample pages, call vision models, inspect audio — whatever is needed to classify the content. Fires based on structural shape.
- **Import**: "Where does this go and how is it named?" Given a semantic classification, decides directory structure, filename convention, and enrichment chain. Reusable across sources that produce the same logical type.
- **Enrichment**: "What can we learn from it?" Runs after filing. Can be light (EXIF extraction) or heavy (transcription, description generation, embedding).

This separation means a scanned notebook arrives as a PDF today and as a folder of JPEGs tomorrow — the same identification and import logic handles both, because what it *is* is independent of what it *looks like* on disk.

### 2.7 Data Agnosticism

The framework makes no assumptions about what types of media the archive contains. There are no hardcoded media categories, file types, or enrichment strategies. All media-type-specific behavior is defined by protocols. The framework provides the structural skeleton (date-based directories, metadata sidecars, the database, the job queue) and protocols fill in the specifics. New and unanticipated media types are first-class citizens — the system learns to handle them through the same conversational process used for any other media.

---

## 3. Architecture Overview

The system has three distinct layers:

```
┌─────────────────────────────────────────────────┐
│                 FRONTIER MODEL                   │
│  (via adapter layer)                             │
│  Decision-making, protocol authoring,            │
│  conversational interaction, self-verification   │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│                  FRAMEWORK                       │
│  CLI interface, job queue, database,             │
│  protocol executor, local tool orchestration     │
│                                                  │
│  ┌────────────┐  ┌──────────┐  ┌─────────────┐  │
│  │  Protocols  │  │ Database │  │  Job Queue  │  │
│  │  (learned)  │  │ (SQLite) │  │             │  │
│  └────────────┘  └──────────┘  └─────────────┘  │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│                   ARCHIVE                        │
│  Filesystem (media files)                        │
│  .meta/ directories (sidecar metadata)           │
│  Human-readable, tool-independent                │
└─────────────────────────────────────────────────┘
```

### Project Directory Structure

The tool's project directory is version-controlled with git.

```
sheaf/                   # git-tracked
├── src/                        # framework source code
├── shapes/                     # learned structural shape definitions (expandable)
│   └── <shape_name>.yaml
├── protocols/                  # learned protocols, organized by layer
│   ├── identification/
│   │   └── <protocol_name>.yaml
│   ├── import/
│   │   └── <protocol_name>.yaml
│   └── enrichment/
│       └── <protocol_name>.yaml
├── scripts/                    # custom scripts installed by the SDK builder
├── config/                     # adapter config, model settings, thresholds
│   ├── adapter.yaml            # which model provider, API keys, etc.
│   ├── thresholds.yaml         # confidence thresholds, maturity settings
│   └── tools.yaml              # registry of installed external tools and models
├── db/                         # SQLite database (the index, not git-tracked)
│   └── archive.db
├── logs/                       # operation logs (partitioned by date)
│   └── 2026-04-04.log
└── ...
```

The archive itself is a separate directory, referenced by the tool via configuration. The archive contains no tool-specific files — only media and `.meta/` metadata.

---

## 4. Archive Structure Specification

### 4.1 Directory Layout

```
<archive_root>/
└── YYYY/
    └── YYYYMMDD/
        ├── <category>/
        │   └── <optional_subcategory>/
        │       └── YYYYMMDD_<protocol_defined_suffix>.ext
        └── .meta/
            └── <category>/
                └── <optional_subcategory>/
                    └── YYYYMMDD_<protocol_defined_suffix>.ext.json
```

The framework enforces the `YYYY/YYYYMMDD/` hierarchy. Everything below `YYYYMMDD/` — the category directories, subcategories, and filename suffixes — is defined by import protocols. The `.meta/` directory mirrors whatever structure the protocols create.

**Example** (illustrative, not prescriptive — categories and subcategories shown here are protocol-defined, not built into the framework):

```
<archive_root>/
└── 2025/
    └── 20250404/
        ├── photo/
        │   └── DMC-TS3/
        │       └── 20250404_1430_IMG_0012.cr3
        ├── scans/
        │   └── 20250404_field_notebook.pdf
        └── .meta/
            ├── photo/
            │   └── DMC-TS3/
            │       ├── 20250404_1430_IMG_0012.cr3.json
            │       └── 20250404_1430_IMG_0012.cr3.embeddings.bin
            └── scans/
                └── 20250404_field_notebook.pdf.json
```

### 4.2 Filename Conventions

The framework enforces `YYYYMMDD_` as the filename prefix for all archived files. What follows the date prefix is determined by the import protocol and varies by media type. Some protocols may append a precise capture time (e.g., `YYYYMMDD_HHMM_<name>`), others a descriptive name (e.g., `YYYYMMDD_<name>`), depending on what is meaningful for that media.

The framework owns the date prefix. The protocol decides the rest of the filename.

### 4.3 Category Directories

The directories within a `YYYYMMDD/` folder are defined by protocols at import time. The framework does not hardcode which categories exist — new categories are introduced naturally as new protocols are created. The framework enforces only that media is organized under `YYYY/YYYYMMDD/<category>/`, with optional further nesting as the protocol requires.

### 4.4 The `.meta/` Directory

Each `YYYYMMDD/` directory contains a hidden `.meta/` directory that mirrors the structure of its parent. For every media file, there is a corresponding `.meta/<path>/<filename>.json` sidecar containing all non-binary metadata. Binary metadata (such as embeddings) is stored in separate files in the same `.meta/` location, referenced from the JSON sidecar.

### 4.5 Sidecar JSON Format

Each sidecar file contains all non-binary metadata associated with its media file. The sidecar is the durable, portable record. The specific fields in a sidecar are determined by the protocols that created and enriched the file. However, every sidecar includes a set of universal fields managed by the framework:

```json
{
  "source_file": "<filename>",
  "capture_date": "YYYY-MM-DD",
  "file_type": "<protocol-defined category>",
  "import_timestamp": "ISO-8601 datetime",
  "imported_by_protocol": "<protocol name>",
  "file_hash": "sha256:<hash>",
  "enrichment_status": {
    "<enrichment_protocol_name>": "complete | pending | failed"
  },
  "protocol_metadata": {
    // protocol-specific fields go here
  },
  "enrichment_data": {
    // enrichment-specific fields go here
  },
  "binary_refs": {
    // references to binary files in the same .meta/ location
    // e.g., "embeddings": "<filename>.embeddings.bin"
  }
}
```

### 4.6 Embedding and Binary Metadata Storage

Embeddings and other large binary metadata are stored in separate files within the `.meta/` directory, alongside the JSON sidecar. The JSON sidecar references these files via the `binary_refs` field. This keeps the JSON sidecar clean, human-readable, and small, while maintaining the principle that all metadata lives near the files it describes.

The database's embeddings table duplicates embedding data for query performance. Both the sidecar binary files and the database embeddings are regenerable by re-running enrichment protocols.

Prototyping should explore embedding dimensionality and model choice to establish the practical tradeoff between quality, processing time, and storage cost. The guiding heuristic: total metadata for a file (JSON sidecar + binary refs) should not exceed the size of the media file itself.

---

## 5. Database Design

### 5.1 Purpose

The SQLite database is a queryable index of the archive. It exists for performance — enabling fast search, filtering, and aggregation that would be slow if done by crawling the filesystem. It is fully rebuildable from the `.meta/` sidecars.

### 5.2 Schema

**Files table** (universal, minimal columns):

| Column | Type | Description |
|---|---|---|
| id | INTEGER PRIMARY KEY | Internal row ID |
| file_path | TEXT UNIQUE | Relative path from archive root |
| capture_date | DATE | Date the media was created |
| file_type | TEXT | Protocol-defined media category |
| file_hash | TEXT | SHA-256 hash for integrity |
| import_timestamp | DATETIME | When the file was imported |
| enrichment_status | TEXT (JSON) | Status of each enrichment protocol |
| imported_by_protocol | TEXT | Name of the import protocol used |

**Metadata table** (flexible, protocol-specific key-value store):

| Column | Type | Description |
|---|---|---|
| file_id | INTEGER FK → files.id | Reference to the file |
| key | TEXT | Metadata key (protocol-defined) |
| value | TEXT | Metadata value |

**Embeddings table** (for vector search):

| Column | Type | Description |
|---|---|---|
| file_id | INTEGER FK → files.id | Reference to the file |
| embedding_model | TEXT | Which model generated the embedding |
| embedding | BLOB | The embedding vector |

The metadata table allows any protocol to store any key-value metadata without schema changes. New protocols introduce new keys naturally. The embeddings table is separate to support experimentation with different embedding models and dimensions, and to allow multiple embeddings per file.

### 5.3 Reindexing

The `reindex` command rebuilds the database from the filesystem:

- **Incremental (default)**: Walk the archive, compare file hashes and modification times to database records, process only new or changed files.
- **Full (`--full` flag)**: Drop and rebuild the entire database from all `.meta/` sidecars.

Incremental reindexing should be lightweight enough to run frequently. Full reindexing is a recovery mechanism.

---

## 6. Protocol System

### 6.1 Four Protocol Layers

The protocol system has four distinct layers. Each layer is fully learned — none are hardcoded in the framework.

---

**Shape** — *"What structural pattern does this source match?"*

Shapes are the cheapest possible analysis: pattern matching against the filesystem before any content is examined. They are defined in `shapes/` as data, not code, and grow as the user shows the tool new types of sources.

A shape captures what makes a structural pattern recognizable:

```yaml
name: image_sequence
description: A flat directory of images with sequential or date-based filenames
indicators:
  - all_same_extension: [.jpg, .jpeg, .png, .tiff, .tif]
  - min_file_count: 3
  - filename_pattern: sequential_or_dated
is_container: false
```

The `is_container` flag indicates whether this shape is a leaf (classifiable) or a container (should be decomposed into sub-units before classification). Mixed directories are containers; a folder of images is a leaf.

---

**Identification** — *"What does this content represent semantically?"*

Identification protocols fire on structural shapes and inspect content — sampling pages, calling vision models, reading file headers — to determine what a file or directory actually *is*. They return a semantic classification and confidence score.

Identification protocols declare which shapes they're willing to examine:

```yaml
name: scanned-notebook-identifier
type: identification
triggers:
  - shape: image_sequence
  - shape: single_document
    extensions: [.pdf, .tiff]
description: Determines whether a document or image sequence is a scanned notebook
```

The key design property: a scanned notebook arrives as a PDF today and as a folder of JPEGs tomorrow. The same identification protocol handles both, because it fires on structural shape — not file extension. The semantic classification it returns ("scanned-notebook") is what gets handed to the import layer.

---

**Import** — *"Where does this go and how is it named?"*

Import protocols receive a semantic classification and decide: category directory, subcategory, filename convention, and enrichment chain. They declare what classifications they accept, not what file extensions they handle:

```yaml
name: scanned-notebook
type: import
accepts_classification: scanned-notebook
category_template: scan
subcategory_template: notebook
filename_template: "{date_start}_{date_end}_{original_name}"
enrichment_chain:
  - protocol_name: notebook-page-ocr
  - protocol_name: document-embeddings
```

The same import protocol handles any physical representation of a scanned notebook, because the identification layer already resolved "what is this?" before import is invoked.

---

**Enrichment** — *"What can we learn from it?"*

Enrichment protocols process files already in the archive. They can be light (EXIF extraction via a shell command) or heavy (OCR, transcription, embedding generation, vision description). Complexity does not determine which layer something belongs to — the layer is determined by *when* it runs relative to filing.

Enrichment protocols execute a shell command that outputs JSON, or invoke the Claude API directly:

```yaml
name: notebook-page-ocr
type: enrichment
media_types: [scan]
output_fields: [text_content, page_count, languages]
method: command
command_template: python3 scripts/ocr_pdf.py "{file_path}"
```

`method: command` is the universal local execution method. The command can call any tool — a local model, a CLI utility, a custom script — as long as it outputs JSON to stdout. The Claude Code SDK builder installs and verifies whatever tooling the command needs. `method: claude` is available for cases where the user explicitly wants API-based enrichment.

### 6.2 The Shape Library

Shapes live in `shapes/` alongside protocols and grow as the user shows the tool new source types. The framework ships with no hardcoded shapes. The first time a truly new structural pattern is encountered, the learning flow asks the user to describe it, drafts a shape definition, and stores it. Subsequent identification protocols can then reference it.

Shapes are cheap enough to check against every incoming source. They gate which identification protocols run — an identification protocol that handles `image_sequence` will never fire on a `device_directory` source, keeping expensive content inspection targeted.

### 6.3 Recursive Decomposition for Mixed Sources

When `sheaf import` is pointed at a large mixed source (a portable drive, a download folder), the framework recursively decomposes it until every sub-unit either has a clear classification or needs user input:

```
classify(path):
  structural_summary = analyze(path)           # cheap: match against shapes/

  best_id = score_identification_protocols(structural_summary)
  if best_id.confidence ≥ threshold:
    return [(path, best_id.classification)]    # leaf — done

  if structural_summary.is_container:
    sub_units = find_logical_groups(path)      # sub-dirs, extension clusters, date groups
    return flatten([classify(u) for u in sub_units])

  return [(path, NEEDS_USER_INPUT)]            # no match — flag for conversation
```

`find_logical_groups` is not a simple directory walk. Within a flat mixed directory it clusters by: file extension families, filename date patterns, and directory boundaries. Each cluster becomes an independent classification attempt. The result for a portable drive is a flat list of `(sub-path, classification)` pairs that are each dispatched to their respective import protocols as independent jobs.

The user sees the plan before anything is filed: "Found 3 things — a camera roll (47 photos), a scanned notebook (1 PDF), and an unrecognized audio folder. Import the first two?"

### 6.4 Tool Registry

`config/tools.yaml` is an auto-managed registry of every external tool, model, and script the system has installed to support enrichment protocols. It grows as new enrichment protocols are built and is consulted during protocol authoring so the model knows what is already available.

```yaml
tools:
  - name: llava
    type: ollama_model
    identifier: llava:latest
    installed_by: photo-description-local
    verified_at: "2026-04-05T14:30:00"
    notes: vision model for image description

  - name: whisper
    type: python_package
    identifier: openai-whisper
    installed_by: audio-transcription
    verified_at: "2026-04-03T09:12:00"

  - name: ocr_pdf.py
    type: custom_script
    identifier: scripts/ocr_pdf.py
    installed_by: notebook-page-ocr
    verified_at: "2026-04-04T16:00:00"
```

Tool types: `ollama_model`, `system_binary`, `python_package`, `custom_script`. When the learning flow determines that new tooling is needed, the Claude Code SDK builder installs and verifies it, then adds an entry to the registry.

### 6.5 Protocol Storage

```
sheaf/
├── shapes/
│   └── <shape_name>.yaml
└── protocols/
    ├── identification/
    │   └── <protocol_name>.yaml
    ├── import/
    │   └── <protocol_name>.yaml
    └── enrichment/
        └── <protocol_name>.yaml
```

### 6.6 Protocol Maturity States

All four protocol types progress through the same maturity states:

- **Draft**: Just created. The model always shows a dry-run preview and asks for user confirmation before executing. Every result is presented for verification.
- **Probationary**: Has been executed and confirmed a few times. Executes autonomously but results are shown for user review after each run.
- **Trusted**: Fully autonomous. Runs without interruption unless something unexpected occurs (errors, confidence below threshold, unrecognized edge cases).

Maturity transitions require explicit user approval. The system suggests promotion when appropriate.

### 6.7 Confidence and Matching

There is a global confidence threshold (configurable in `config/thresholds.yaml`) and per-protocol overrides. The threshold applies at both the identification layer (does this classification meet the bar?) and, for the first stage of matching, at the shape layer (does this source match a known shape?).

If the best identification result falls below threshold, the system enters conversational mode. If no shape matches at all, the framework presents its structural findings to the user and begins the learning flow to define a new shape and identification protocol.

### 6.8 Protocol Introspection

The `sheaf protocols show <name>` command presents a complete summary of any protocol across all four layers: name, type, maturity with run count, trigger conditions, what it produces, what it hands off to, and last run date.

The `sheaf protocols explain <name>` command gives a conversational plain-language explanation of what the protocol does and why it was designed that way.

---

## 7. The Learning Flow

### 7.1 Overview

The learning flow is the core interaction pattern of the system. It governs how the tool handles new sources and new enrichment needs. A single conversational session for a genuinely new media type may define all four protocol layers — shape, identification, import, and enrichment — plus any external tooling needed to support them.

### 7.2 The Import Cycle

```
User runs: sheaf import /path/to/source
                    │
                    ▼
        ┌─── Structural analysis ──────────────┐
        │  Match source against known shapes   │
        │  Decompose containers recursively    │
        │  Produce list of (path, shape) units │
        └───────────┬──────────────────────────┘
                    │
          ┌─────────▼──────────────────────────┐
          │  Run identification protocols       │
          │  (filtered to matching shapes)      │
          │                                     │
          │  Classification confidence ≥        │──── YES ──→ Dispatch to import protocol
          │  threshold?                         │             (respecting maturity state)
          └─────────┬───────────────────────────┘
                    │ NO
                    ▼
          Enter conversational mode
          (may define new shape + identification
           + import + enrichment protocols)
```

### 7.3 Learning Conversation for a New Media Type

When the system cannot classify a source, it enters a conversational session. A fully new media type typically results in all four layers being defined:

1. **Structural investigation**: The model examines the source — directory layout, file types, filename patterns, any available metadata. It presents its findings and checks whether any existing shapes match.

2. **Shape definition** (if needed): If the source doesn't match any known shape, the model proposes one: "This looks like a flat directory of sequentially named images — I'll call that `image_sequence`. Does that make sense?" The shape is stored in `shapes/`.

3. **Identification protocol**: The model determines how to classify the content. For a simple case (DCIM layout clearly means camera roll) this may be trivial. For ambiguous cases (is this PDF a notebook or a receipt?), it designs a content sampling approach — extracting pages, calling a vision model — and drafts an identification protocol.

4. **Import protocol**: With classification settled, the model asks how files should be filed: category, subcategory, filename convention, date source. It previews the result with sample files and iterates until confirmed.

5. **Enrichment protocol(s)**: After import is defined, the conversation turns to enrichment. The model checks existing enrichment protocols for precedent, suggests applicable ones, and drafts new ones where needed. For enrichment that requires new tooling (a model, a CLI tool, a custom script), the model proposes installing it: "This protocol needs X — want me to set that up?" On confirmation, the Claude Code SDK builder installs and verifies the tooling and returns a `command_template`.

6. **Execution**: Import runs in draft maturity. Results are shown for verification before the session closes.

### 7.4 The Claude Code SDK Builder

When enrichment protocol setup requires installing external tooling — a local model, a Python package, a system binary, or a custom script — the conversational session invokes a Claude Code SDK agent as a tool. The agent:

- Installs whatever is needed using the system's package manager, pip, or other tooling.
- Writes and tests any custom scripts.
- Verifies that the tool works correctly on a sample file.
- Returns a verified `command_template` and a list of tool registry entries to add to `config/tools.yaml`.

The SDK agent's output streams inline to the terminal so the user can watch it work. The framework is tool-agnostic — the SDK agent can install anything; the enrichment protocol only cares that its `command_template` outputs valid JSON.

### 7.5 Self-Verification and Debugging

Throughout the learning flow and during all protocol execution:

- After saving an enrichment protocol, the system automatically runs it against a sample file and presents the output: "Here's what this produces — does it look right?"
- The model actively checks results against what was agreed upon: "You said the filenames should look like X — here's what I produced. Does this match?"
- If a protocol fails during execution, the model attempts to diagnose the issue and fix it autonomously. If it cannot resolve the issue, it resurfaces the problem to the user with context.
- New and probationary protocols always include result verification. The model presents a sample of outputs for the user to confirm.
- The model is especially cautious with filesystem operations. It never overwrites, and it previews destructive actions before executing.

### 7.6 Protocol Evolution

Protocols are living documents. As the user encounters edge cases or changes preferences, existing protocols can be revised through the same conversational process (`sheaf protocols edit <name>`). The model can suggest protocol updates when it notices patterns: "The last few imports from this source had files I hadn't seen before — should I update the protocol to handle these?"

Shapes can also evolve. If an existing shape definition no longer accurately describes what the user is seeing, it can be updated through the same editing flow.

---

## 8. Job Queue and Async Processing

### 8.1 Purpose

Many operations — especially enrichment — are long-running. The job queue allows these to run in the background while the user continues other work. It also provides the mechanism for resurfacing tasks that need user input.

### 8.2 Job Lifecycle

```
queued → processing → [needs-review | complete | failed]
```

- **Queued**: Job is waiting to be processed.
- **Processing**: Job is actively running (local model inference, file operations, etc.).
- **Needs-review**: Job has completed but requires user input before finalizing (e.g., confirming outputs from a probationary protocol, reviewing results of a complex enrichment).
- **Complete**: Job finished successfully, no further action needed.
- **Failed**: Job encountered an error. Includes error context for debugging.

### 8.3 Resurfacing

When a job transitions to `needs-review`, it becomes visible in the `sheaf jobs` queue. The user checks in when they have time, selects a job, and enters a conversational session with the model to resolve it. This is the same chat interaction pattern used during import — chat mode is the universal interface whenever the system needs user input.

Future enhancement (out of scope for v1): email or system notifications when jobs need attention.

### 8.4 Background Processing

Enrichment jobs can run for extended periods (hours or days for large imports). The system should:

- Process jobs sequentially or with configurable parallelism.
- Be resilient to interruption — jobs can be paused and resumed.
- Report progress on long-running jobs when the user checks status.

---

## 9. Filesystem Safety and Version Control

### 9.1 Technical Guardrails

- **No overwrites**: If a file already exists at the destination path, the operation halts and queues for user review. No silent overwriting ever occurs.
- **Copy by default**: Source files are never deleted or moved unless the user explicitly opts in. The default is to copy into the archive.
- **Dry-run previews**: All destructive or novel operations are previewed before execution. The user sees exactly what will happen before it happens.

### 9.2 Agentic Self-Verification

Because the frontier model operates with significant autonomy, safety is not just about preventing accidents — it's about the model actively validating its own behavior:

- During protocol prototyping, the model asks: "Did this come out like I think it would? Did this come out like we discussed?"
- The model compares actual results against the protocol's declared expectations.
- For probationary protocols, the model presents a sample of results for user verification after every run.
- The model surfaces anomalies: unexpected file counts, unusual file sizes, missing metadata, timestamps that don't make sense.

### 9.3 Operation Log

The operation log is the sole rollback mechanism for the archive. It records every change the tool makes to the archive's filesystem — media files, `.meta/` sidecars, and binary metadata — with enough detail to reverse any operation.

#### Structure

The operation log is stored in the tool's project directory under `logs/`, partitioned by date for manageability (e.g., `logs/2026-04-04.log`). Each entry is a **transaction** representing one logical operation (an import run, an enrichment pass, etc.). A transaction contains one or more **actions**.

Each action records:

- **Action type**: `file_created`, `file_moved`, `file_deleted`, `sidecar_created`, `sidecar_updated`, `binary_meta_written`
- **Paths**: Source and destination paths as applicable
- **Timestamp**: When the action occurred
- **Protocol**: Which protocol initiated the action
- **Prior state snapshot** (conditional): The previous content of a file, if needed for rollback

#### Two Tiers of Detail

Not all actions require the same level of logging:

**Full snapshots** for human-touched or non-regenerable changes. When a sidecar is updated with information that involved human input — confirmed tags, reviewed outputs, manually approved results — the log stores a snapshot of the sidecar's previous state. This is critical because human decisions cannot be regenerated by re-running a protocol. Sidecar files are small (typically 1-5KB), so snapshot storage is negligible.

**Lightweight action records** for machine-generated, regenerable outputs. When an enrichment protocol writes embeddings, generates tags via a local model, or produces other machine outputs, the log records what happened (file path, action type, protocol) but does not snapshot the prior state. If rollback is needed, the previous machine-generated output is simply deleted — it can always be regenerated by re-running the enrichment protocol.

This tiered approach keeps the operation log lean. Estimated storage: under 100MB per year for an active archive with thousands of imports and enrichment runs.

#### Rollback

Rollback operates at the transaction level. Reversing a transaction undoes all of its actions in reverse order:

- `file_created` → delete the file
- `file_moved` → move the file back
- `sidecar_created` → delete the sidecar
- `sidecar_updated` → restore from snapshot (if snapshot exists) or delete the updated fields
- `binary_meta_written` → delete the binary metadata file

### 9.4 Version Control Strategy

Git is used exclusively for the tool's project directory — never for the archive itself. The archive remains clean: only media files and `.meta/` sidecars, no tool artifacts.

**What git tracks:**

- Source code (`src/`)
- Protocols (`protocols/`)
- Configuration (`config/`)
- Operation logs (`logs/`)

The git history of `protocols/` serves as a changelog of how the system has learned over time. The git history of `logs/` provides a versioned backup of the rollback mechanism itself.

**What git does not track:**

- The archive filesystem (media files, `.meta/` sidecars)
- The SQLite database (rebuildable from the archive)

This separation ensures the archive remains tool-independent. If the tool is removed, the archive is unaffected. Rollback of archive changes is handled entirely by the operation log, not by git.

### 9.5 Automated Log Management

The framework manages the operation log automatically. The user never interacts with log files directly. Every framework action that touches the archive — import, enrichment, metadata updates — is wrapped in a transaction that is written to the operation log as part of the same operation. This is not a separate step the user or the model needs to remember; it is built into the framework's file operation layer.

---

## 10. CLI Interface and Commands

### 10.1 Command Overview

The tool is invoked as `sheaf` with subcommands. Chat mode activates automatically whenever the system needs user input.

### 10.2 Commands

#### `sheaf import <path>`

Point at a file, directory, or mounted device. Initiates the recognition → import → enrichment flow.

- If a matching protocol exists with sufficient confidence and is trusted: executes automatically, reports results.
- If a matching protocol exists but is draft or probationary: executes with verification steps.
- If no protocol matches: enters chat mode for the learning flow.

Flags:
- `--dry-run`: Preview what would happen without making changes.

#### `sheaf jobs`

Displays the job queue. Shows jobs grouped by status: needs-review first, then processing, queued, recently completed, failed.

- Interactive: select a job that needs review to enter a chat session for resolution.
- Flags: `--status <status>` to filter, `--protocol <name>` to filter by protocol.

#### `sheaf search <query>`

Search the archive by content, metadata, or semantic similarity.

- Default: semantic search across all enrichment data (descriptions, tags, transcriptions).
- Flags:
  - `--fuzziness <0-100>`: Controls the similarity threshold for semantic search. 0 = exact match, 100 = loosely related.
  - `--date <YYYYMMDD>` or `--date <start>..<end>`: Filter by date or date range.
  - `--type <type>`: Filter by media category.
  - `--meta <key>=<value>`: Filter by protocol-specific metadata.
  - `--browse`: Open results in the web GUI instead of listing in the terminal.

#### `sheaf browse`

Launches the local web server and opens the camera roll GUI in a browser. When launched without additional context, shows a chronological view of all media with basic filters.

Can be invoked internally by `search --browse` to display search results in the GUI.

#### `sheaf reindex`

Rebuilds the database from the filesystem and `.meta/` sidecars.

- Default: incremental (only processes new or changed files based on file hash and modification time).
- Flags: `--full` for a complete rebuild from scratch.

#### `sheaf status`

Quick dashboard of the archive:

- Total files, breakdown by category.
- Pending jobs and jobs needing review.
- Last import date.
- Protocol count and maturity breakdown.

#### `sheaf protocols`

Manage and inspect learned protocols across all four layers.

- `sheaf protocols list`: Show all protocols (all types) with maturity and last run date.
- `sheaf protocols show <name>`: Structured summary of a protocol.
- `sheaf protocols explain <name>`: Conversational plain-language explanation of what a protocol does and why.
- `sheaf protocols new [--type TYPE] [--source PATH]`: Start a conversational session to create a new protocol. `TYPE` is one of `identification`, `import`, or `enrichment`. For import protocols, `--source` points at a sample source to investigate.
- `sheaf protocols edit <name>`: Re-enter a conversational session to revise an existing protocol. The model is seeded with the current definition and proposes changes based on what the user describes.
- `sheaf protocols test <name> <source>`: Dry-run a protocol without making changes. For import protocols, shows the preview filing table. For enrichment protocols, runs against a sample file and shows the raw output.
- `sheaf protocols delete <name>`: Remove a protocol (with confirmation).

Shapes are managed separately:

- `sheaf shapes list`: Show all known structural shapes.
- `sheaf shapes show <name>`: Show a shape definition.

#### `sheaf verify`

Check archive integrity:

- Every media file has a corresponding `.meta/` sidecar.
- Every sidecar has a corresponding database entry.
- No orphaned database records (files that no longer exist on disk).
- File hashes match between sidecar records and actual files.
- Reports discrepancies and optionally repairs them.

#### `sheaf history`

Browse and interact with the operation log.

- `sheaf history`: Show recent operations (transactions), most recent first.
- `sheaf history --date <YYYYMMDD>`: Show operations from a specific date.
- `sheaf history --protocol <name>`: Show operations from a specific protocol.
- `sheaf history show <transaction-id>`: Detailed view of a specific transaction — all actions, files affected, whether snapshots exist.
- `sheaf history rollback <transaction-id>`: Reverse a specific transaction. Previews the rollback actions before executing and requires confirmation.

---

## 11. Search and Access

### 11.1 Search Architecture

Search combines traditional metadata filtering with semantic vector search:

- **Metadata search**: Exact and pattern matching on the files and metadata tables. Fast, handled directly by SQLite queries.
- **Semantic search**: Vector similarity search against the embeddings table. Returns results ranked by cosine similarity to the query embedding.
- **Combined**: Filters narrow the candidate set (by date, category, metadata), then semantic search ranks within that set.

### 11.2 Fuzziness Control

The fuzziness parameter controls the similarity threshold for semantic search:

- Low fuzziness (0): Only returns files whose content closely matches the query. A search returns direct matches.
- High fuzziness (100): Returns files with tangentially related content. A search returns both direct matches and conceptually adjacent results.

Implementation: the fuzziness value maps to a similarity threshold on the vector search. Lower fuzziness = higher similarity threshold (stricter). Higher fuzziness = lower similarity threshold (more permissive).

### 11.3 CLI Search Output

Search results in the CLI display:

- File path (relative to archive root).
- Capture date.
- Relevance score (for semantic search).
- A brief description or tag summary from enrichment metadata.

---

## 12. Camera Roll GUI

### 12.1 Overview

A local web application served from `localhost` that provides a visual browsing experience for the archive. The GUI is intentionally barebones for v1 — the user will discover how they want to use it through actual use.

### 12.2 Core Features

- **Thumbnail grid**: Chronological display of media thumbnails. The appropriate thumbnail representation is determined by media type.
- **Adjustable thumbnail size**: A control to change the grid density (small thumbnails = more visible at once, large thumbnails = more detail).
- **Scrolling**: Smooth chronological scrolling through the entire archive or a filtered subset.
- **Date filtering**: Filter the view to a specific date or date range.
- **Search integration**: When launched via `search --browse`, displays search results instead of the full roll. Search is also available within the GUI.
- **Media playback**: Clicking a thumbnail opens the full-resolution media with appropriate playback for its type.
- **Media detail view**: Clicking any item shows the full-resolution media alongside its metadata.

### 12.3 Design Philosophy

The GUI is a flexible viewer. It can display the full chronological archive, a filtered subset, or search results. It is not the primary interface for the tool — the CLI is. The GUI exists specifically because visual media browsing is fundamentally a visual task that benefits from a graphical interface.

Future enhancements (out of scope for v1): remote access, tagging/editing from the GUI, batch operations.

---

## 13. Frontier Model Integration

### 13.1 The Adapter Layer

The adapter is the boundary between the framework and any specific LLM provider. The framework communicates exclusively through the adapter interface and never interacts with a provider's API directly.

### 13.2 Adapter Responsibilities

**Message formatting**: Translates the framework's internal message format into the provider's expected format and translates responses back into a standard internal format.

**Tool definitions**: The framework defines tools in a standard, provider-agnostic format (tool name, parameters, description). The adapter translates these into the provider's specific tool/function calling format. Tool implementations (Python functions, shell commands) remain unchanged — only the descriptions are reformatted.

**System prompts**: Stored as plain text templates in the project's `config/` directory. The adapter formats them appropriately for the target provider. Prompts are written in clear, direct natural language without provider-specific tricks or formatting.

**Capabilities negotiation**: The adapter exposes a capabilities object describing what the current model supports:

```
adapter.capabilities → {
    vision: bool,       # can the model see images?
    tool_use: bool,     # does it support tool/function calling?
    max_context: int,   # context window size
    streaming: bool     # does it support streaming responses?
}
```

If the framework needs a capability the current model lacks (e.g., vision), it can fall back to processing via local tools first (e.g., describe an image with a local vision model and pass the description as text).

**Conversation management**: The adapter manages conversation history in the format the provider expects and handles context window limits.

**Error recovery**: The adapter handles API-level errors simply and transparently:

- Retries on transient failures (rate limits, timeouts) with backoff.
- Surfaces persistent failures to the framework as structured errors.
- The framework can then decide to queue the task for later or escalate to the user.

Error recovery should be simple and not bloat the adapter. The goal is resilience, not sophistication.

### 13.3 Adapter Interface

The adapter exposes a minimal interface to the framework:

```
adapter.chat(messages, tools, options) → response
adapter.capabilities → { ... }
```

All complexity lives behind this interface. The Claude adapter knows about Anthropic's API format, authentication, and quirks. A future adapter for another provider would handle theirs. The framework only ever calls `adapter.chat()`.

### 13.4 Claude Code SDK Builder

For protocol setup that requires installing external tooling, the conversational session has access to a `build_protocol_tooling` tool that spawns a Claude Code SDK agent. This is a distinct capability from the frontier model adapter: the adapter handles conversational turns, while the SDK builder handles agentic execution with full tool access (shell commands, file writes, package installation).

The SDK builder:
- Receives a task description, media context, and the current tool registry.
- Spawns a Claude Code SDK agent with `acceptEdits` permission mode and a restricted tool set (`Bash`, `Read`, `Write`, `Edit`).
- Streams the agent's output inline to the terminal during execution.
- Returns a verified `command_template` plus new tool registry entries.
- Updates `config/tools.yaml` with the installed tools.

The framework is entirely tool-agnostic at the enrichment layer — any model, binary, package, or script can be installed through this mechanism. The only contract is that the resulting `command_template` outputs valid JSON to stdout.

### 13.5 v1 Implementation

For v1, only the Claude adapter is built. The abstract interface is defined so that building additional adapters in the future is straightforward — the contract a new adapter must fulfill is clear and documented.

---

## 14. Prototyping Priorities and Open Questions

### 14.1 Prototyping Priorities

These are the areas that need hands-on experimentation before the design can be fully finalized:

1. **Shape library bootstrap**: Define the initial set of shapes through real imports rather than speculation. Each new import type encountered in practice should produce a shape definition. Let the library grow organically.

2. **Identification protocol design**: Build 2-3 identification protocols for real media types (camera roll, scanned document, audio session) and see what the format needs to support. The distinction between "trivial identification" (DCIM layout = camera roll, no content inspection needed) and "content-sampling identification" (PDF = ???, needs page inspection) should emerge from real cases.

3. **Recursive decomposition heuristics**: Test `find_logical_groups` against real mixed sources. The clustering logic (by extension family, date patterns, directory boundaries) needs tuning against actual portable drive contents.

4. **Embedding storage**: Experiment with embedding dimensionality, model choice, and resulting file sizes. Validate that binary sidecar files in `.meta/` are practical at scale.

5. **Local model selection**: Evaluate local models for each class of enrichment task — OCR, content description, audio transcription, embedding generation. Prioritize accuracy over speed.

6. **Confidence threshold calibration**: Tune thresholds at both the shape-matching and identification layers. Too low = acts on bad classifications. Too high = asks the user too often.

### 14.2 Open Questions

- **Identification protocol scope**: Does one identification protocol handle a single media type (notebook identifier, receipt identifier) or can one protocol return multiple possible classifications with different confidences? The latter is more powerful but harder to author.
- **Shape vs. identification boundary**: For very clear structural signals (DCIM layout), should identification be skipped entirely and the import protocol dispatched directly from shape matching? Or should identification always run, even if trivially fast?
- **Per-file vs. per-source classification**: Identification currently operates on a unit (a directory or file). For mixed flat directories, `find_logical_groups` clusters by extension before identification runs. Should identification protocols ever be asked to classify a heterogeneous set of files and route them differently, or is pre-clustering always sufficient?
- **Database schema for embeddings**: Should the embeddings table support multiple embeddings per file (e.g., different models, different regions of a document)?
- **Thumbnail generation**: Should the import pipeline generate thumbnails, or should the GUI generate them on-the-fly? Pre-generation is faster at browse time but adds to import time and storage.
- **Maturity promotion thresholds**: How many successful runs before suggesting promotion from draft to probationary, or probationary to trusted? Should this be configurable per protocol?
- **Multi-file enrichment protocols**: Some enrichment operates on one file and produces many outputs (a PDF that gets split into per-page records). Does the protocol format need to handle this explicitly?
