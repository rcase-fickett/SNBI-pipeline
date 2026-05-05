# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Automated extraction of SNBI (Special Notice Bridge Inspection) data items for ~600 ODOT bridges. The pipeline reads BrM (Bridge Management) Excel exports and bridge plan PDFs, uses the Claude API to extract SNBI item values from plan images, stores results in a SQLite database, and serves a local Flask web UI for review.

## Running the Pipeline

All scripts must be run from the project root with the Anthropic API key in the environment.

```bash
# Full pipeline — run in order
python 01_init_db.py                      # Phase 1: load BrM Excel → DB (no API)
python 06_import_grid.py                  # Phase 1b: load BrM Grid Export (struct types, spans, lat/long)
python 02_process_bridges.py              # Phase 2: extract via Claude API
python 02_process_bridges.py --limit 5   # test on 5 bridges
python 02_process_bridges.py --id 02283  # one specific bridge
python 03_export_csv.py                   # Phase 3: export CSV for Google Sheets
python 04_import_corrections.py --csv "snbi_evidence_export.csv"  # Phase 4: import inspector feedback
python 04_import_corrections.py --csv "snbi_evidence_export.csv" --dry-run  # preview first
python 05_build_lessons.py               # Phase 5: distil lessons from corrections
python 05_build_lessons.py --show-lessons  # view active lessons
python 07_import_bridge_log.py           # Phase 7: import clearance pre-fills from brlog.pdf
python 07_import_bridge_log.py --dry-run # preview without writing
python 07_import_bridge_log.py --sample  # dump raw PDF text from first 3 pages
python 08_import_infobridge.py           # Phase 8: import NBI pre-fills from InfoBridge export
python 08_import_infobridge.py --dry-run # preview without writing
python 09_discover_features.py           # Phase 9: GIS enrichment via ODOT TransGIS + OSM
python 09_discover_features.py --dry-run --limit 10  # test on 10 bridges
python 09_discover_features.py --bridge 02283 --verbose  # single bridge, verbose

# Monitor
python status.py
python status.py --lessons
python status.py --flagged
python status.py --errors
```

## Web UI

```bash
python app.py        # starts Flask dev server at http://localhost:5000
```

Or use the desktop launcher (see Deployment below).

Pages:
- `/` — Dashboard (bridge counts, evidence stats)
- `/review` — Review queue (filterable evidence table)
- `/bridge/<id>` — Per-bridge evidence review with inline PDF viewer and map
- `/process` — Trigger Phase 2 processing jobs and feedback cycles
- `/items` — Item Coding Guide: source chain and priority rule for every SNBI item

The UI can trigger Phase 2 processing and the feedback cycle (Phases 4+5) in the background via `/api/process` and `/api/feedback`.

## Deployment (Team Launcher)

The app is distributed as a click-to-launch desktop icon — no terminal required after first-time setup.

**Files:**
- `launch.vbs` — Silent launcher. Checks if first-time setup is done; if so starts the Flask server as a hidden background process and opens the browser automatically.
- `setup_and_start.ps1` — Setup + start script. Checks for Python, installs packages, prompts for API key and bridge folder path (once per machine), then starts the server. Accepts `-Silent` flag (used by `launch.vbs`) to skip all output and prompts.
- `Create Desktop Shortcut.bat` — Run once per machine to drop an "SNBI Review" icon on the Desktop. Double-click to run — no right-click needed.

**First-time setup on a new machine:**
1. Open the project folder on OneDrive
2. Double-click `Create Desktop Shortcut.bat`
3. Double-click the new "SNBI Review" icon on the Desktop
4. Enter the Anthropic API key and bridge folder path when prompted
5. Browser opens automatically — setup is saved for all future launches

**Gotcha:** If the server fails to restart, check for stale Python processes with `netstat -ano | findstr :5000` and kill them via Task Manager or `Stop-Process`.

## Configuration

`config.py` is the single source of truth. Key settings:
- `BRIDGES_ROOT` — overridden by `SNBI_BRIDGES_ROOT` env var (set by `setup_and_start.ps1` per team member)
- `CLAUDE_MODEL` — currently `claude-sonnet-4-6`
- `BATCH_DELAY_SEC` — rate limit buffer between API calls
- `BRIDGE_FILTER` — set to a list of bridge IDs to restrict processing

Bridge PDFs are expected at: `{BRIDGES_ROOT}/{bridge_id}/{bridge_id} Plans.pdf`

## Architecture

### Data Flow

```
BrM Excel exports
  → 01_init_db.py / 06_import_grid.py       Phase 1: BrM seed (features, BrM values)
  → 07_import_bridge_log.py                 Phase 7: brlog.pdf clearance pre-fills (APPROX)
  → 08_import_infobridge.py                 Phase 8: InfoBridge NBI pre-fills (APPROX)
  → 09_discover_features.py                 Phase 9: GIS enrichment — B.F.03 names, B.RR.01,
                                                       P01/P02 pathways, divided-highway flags
  → SQLite (snbi_evidence.db)
  → 02_process_bridges.py (Claude API, PDF images)  Phase 2: plan extraction (HIGH)
  → evidence table (plan_value, plan_confidence, plan_reasoning)
  → app.py (Flask review UI) / 03_export_csv.py (CSV)
  → Inspector corrections (04_import_corrections.py)
  → Lessons distilled by Claude (05_build_lessons.py)
  → Injected back into 02_process_bridges.py prompts
```

### Database Tables

| Table | Purpose |
|---|---|
| `bridges` | One row per bridge — status, PDF paths, lat/long |
| `evidence` | One row per bridge × item × feature — BrM + plan values + inspector fields |
| `corrections` | Raw inspector corrections keyed to evidence rows |
| `lessons` | Versioned, distilled lessons per SNBI item (injected into Phase 2 prompts) |
| `processing_log` | Per-bridge processing log |
| `import_batches` | Audit log of Phase 4 import runs |

### Key Modules (`lib/`)

- **`snbi_items.py`** — Master list of all SNBI items with BrM column mappings, page type routing, and extraction notes. This is the authoritative source for what gets extracted and from which plan page types.
- **`db.py`** — All SQLite operations. `get_conn()` enables WAL mode and foreign keys. `migrate_db()` adds columns non-destructively.
- **`claude_api.py`** — Builds prompts and calls the Anthropic API. `ClaudeExtractorWithLessons` injects active lessons into each extraction call. Reference docs (SNBI errata PDF + datacrosswalk) are sent as context.
- **`pdf_extractor.py`** — Renders PDF pages to JPEG images via pypdfium2. Uses `metadata.json` in each bridge folder to route pages to the correct page type (PLAN, SECTION, RAIL, etc.).
- **`results_merger.py`** — Parses Claude API JSON output and upserts into the `evidence` table.
- **`brm_loader.py`** — Reads BrM Excel exports and populates the `bridges` and `evidence` tables with BrM-derived values.
- **`geo_context.py`** — Two roles: (1) `build_context_block()` builds GIS context for Claude extraction prompts; (2) `discover_features()` / `get_bridge_coords()` are used by `09_discover_features.py` for DB enrichment. Queries ODOT TransGIS layers 101/132/136/143/164/166/377 and OSM Overpass. No API key required.

### Evidence Confidence Levels

`HIGH` | `APPROX` | `FIELD_REQ` | `NA` | `PENDING`

### Feature IDs

Evidence rows use `feature_id` to distinguish: `PRIMARY` (bridge-level), `WORK:YYYY` (rehab work), `H01/H02` (highway features), `W01` (waterway), `R01` (railroad), `P01` (sidewalk), `P02` (bicycle facility). B.F.02='C' means carried on the bridge; 'B' means below. `get_below_features(conn, bridge_id, prefix)` in `lib/db.py` queries B.F.02='B' dynamically — use it instead of hardcoding H02.

## Source Control

Code is versioned at `https://github.com/rcase-fickett/SNBI-pipeline` (private). The database (`snbi_evidence.db`), Excel exports (`*.xlsx`), PDFs, and CSV outputs are excluded via `.gitignore` — those are backed up by OneDrive only. To push after making changes: `git add . && git commit -m "..." && git push`. GitHub account is `rcase-fickett` — credentials are stored in Windows Credential Manager.

## Important Constraints

- Phase 2 is resumable — re-running skips bridges with `PLANS_DONE` status. Use `--reprocess` flag to force re-extraction.
- `06_import_grid.py` only fills null BrM values — it will not overwrite existing data.
- Two SQLite databases exist: `snbi_evidence.db` (active) and `snbi_evidence-ZBook-S.db` (backup from another machine). Always operate on `snbi_evidence.db`.
- `brlog.pdf` (ODOT Bridge Log 2024) in project root is parsed by `07_import_bridge_log.py`. It pre-fills B.H.13/B.H.14/B.H.15 as APPROX brm_values for the H02 (below-highway) feature. H01 is always the carried feature (99.9 pre-filled by Phase 1 — bridge log clearances never apply to it).
- The `datacrosswalk.xlsx` and `SNBI March 2022 Errata 01.pdf` are sent as reference context to Claude on every Phase 2 call — keep them in the project root.
- `08_import_infobridge.py` reads the most recent `Selected_Bridges_*.txt` in the project root (auto-selected by filename sort). ID mapping: `BrM.BridgeNumber == InfoBridge "8 - Structure Number"` (strip quotes). NBI 42B routes underclearances to H*/R*/W* features. 1178 of 1184 bridges match; 6 misses are Forest Service bridges.
- `09_discover_features.py` is idempotent — only fills null plan_values. Run after phases 1/7/8 and before phase 2 so plan extraction can override GIS APPROX values with HIGH. Pauses 0.3s between bridges as server courtesy.
