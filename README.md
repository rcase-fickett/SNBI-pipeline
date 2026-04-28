# SNBI Pipeline

Automated extraction of SNBI data items from BrM exports and bridge plan PDFs.
Outputs a CSV for import into Google Sheets / AppSheet for field verification.

---

## Prerequisites

- Python 3.9+
- An Anthropic API key (for Phase 2 only)
- Plan PDFs organised as: `{BRIDGES_ROOT}/{bridge_id}/{bridge_id} Plans.pdf`
- Optional: `metadata.json` in each bridge folder (enables smart page routing)
- Optional: `{bridge_id}_BC_*.pdf` vertical clearance docs in each bridge folder

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your Anthropic API key (Windows)
setx ANTHROPIC_API_KEY "sk-ant-api03-..."
# (restart your terminal after this)

# 3. Edit config.py — set BRIDGES_ROOT and file paths
```

---

## Running the Pipeline

### Phase 1 — Load BrM data (no API calls, ~1 minute)
```bash
python 01_init_db.py
```
Reads the Excel files, builds the SQLite database, and pre-populates all BrM-derived
values. Run this once. Safe to re-run — it uses INSERT OR REPLACE.

### Phase 2 — Process PDFs via Claude API (~2-3 min per bridge)
```bash
python 02_process_bridges.py             # all pending bridges
python 02_process_bridges.py --limit 5  # test on 5 bridges first
python 02_process_bridges.py --id 02283 # one specific bridge
```
For each bridge, selects the most relevant plan pages based on metadata.json,
sends them to the Claude API, and stores the extracted values.

**Cost estimate:** ~8 pages per bridge × 600 bridges = ~4,800 API calls.
At claude-sonnet-4 rates (~$0.003/image), budget ~$15-20 total.

**Rate limits:** The default 1-second delay between calls is conservative.
You can reduce `BATCH_DELAY_SEC` in config.py if you're on a higher API tier.

**Resumable:** If interrupted, re-running will skip already-processed bridges.
Use `--reprocess` to re-run bridges that errored.

### Phase 3 — Export to CSV for Google Sheets (instant)
```bash
python 03_export_csv.py
```
Generates three CSV files:
- `snbi_evidence.csv` — main evidence table → import to Google Sheets
- `missing_bridges.csv` — bridges not yet in your AppSheet bridge table
- `snbi_summary.csv` — one row per bridge with processing stats

---

## Importing to Google Sheets / AppSheet

1. Open your project Google Sheet
2. Create a new tab: **snbi_evidence**
3. File → Import → Upload `snbi_evidence.csv` → "Replace current sheet"
4. In AppSheet Editor:
   - Data → Add table → select the new `snbi_evidence` tab
   - Set `evidence_id` as the key column
   - Add a Ref column: `bridge_id` → your existing bridge table
   - Add column types: `plan_confidence` and `status` as Enum
   - Make `field_value`, `field_notes`, `status` editable by inspectors
   - Make all other columns read-only

---

## Evidence Table Schema

| Column | Description |
|--------|-------------|
| `evidence_id` | Unique key: `{bridge_id}-{item_id}-{feature_id}` |
| `bridge_id` | Links to your bridge table |
| `item_id` | SNBI item (e.g. B.G.01) |
| `feature_id` | PRIMARY, H01, W01, R01, WORK:1982, etc. |
| `item_name` | Human-readable item name |
| `item_table` | PRIMARY / WORK / FEATURE |
| `brm_value` | Value from BrM export (pre-filled) |
| `plan_value` | Value extracted from plans (pre-filled) |
| `plan_confidence` | HIGH / APPROX / FIELD_REQ / NA / PENDING |
| `plan_reasoning` | How the plan value was derived |
| `plan_source_pages` | Which drawing it came from |
| `auto_questions` | Questions generated during extraction |
| `field_value` | **Inspector fills this** |
| `field_notes` | **Inspector notes / corrections** |
| `status` | PENDING / REVIEWED / FLAGGED / APPROVED |

---

## Confidence Levels

| Level | Meaning |
|-------|---------|
| `HIGH` | Clearly shown on plans or directly from BrM |
| `APPROX` | Derived or calculated — verify in field |
| `FIELD_REQ` | Cannot be determined from plans — must measure in field |
| `NA` | Not applicable per SNBI spec |
| `PENDING` | Not yet extracted |

---

## Folder Structure

```
snbi_pipeline/
  config.py              ← EDIT THIS
  requirements.txt
  01_init_db.py          ← Run first
  02_process_bridges.py  ← Run second
  03_export_csv.py       ← Run to generate output
  snbi_evidence.db       ← Generated SQLite database
  lib/
    __init__.py
    snbi_items.py        ← SNBI item definitions
    db.py                ← Database operations
    brm_loader.py        ← BrM Excel loading
    pdf_extractor.py     ← PDF page rendering
    claude_api.py        ← Claude API calls + prompts
    results_merger.py    ← Merges API results into DB
```

---

## Feedback Cycle (Phases 4 & 5)

The pipeline gets smarter with each review cycle. After inspectors review
evidence rows in AppSheet, their corrections are imported back and distilled
into lessons that improve future extractions.

### Phase 4 — Import Inspector Corrections
```bash
# Export snbi_evidence sheet from Google Sheets as CSV, then:
python 04_import_corrections.py --csv "snbi_evidence_export.csv"

# Preview without writing (recommended first run):
python 04_import_corrections.py --csv "snbi_evidence_export.csv" --dry-run
```
Reads every row where `status` is REVIEWED / FLAGGED / APPROVED.
Compares `field_value` vs `plan_value` and classifies each as:
- `CONFIRMED` — inspector agreed with AI value (positive signal)
- `VALUE_WRONG` — AI value was incorrect
- `NA_WRONG` — AI marked item N/A but it actually applies
- `CONFIDENCE_WRONG` — value OK but confidence level wrong

### Phase 5 — Build Lessons
```bash
python 05_build_lessons.py                  # all items with ≥2 new corrections
python 05_build_lessons.py --min-corrections 1   # lower threshold
python 05_build_lessons.py --item B.G.01    # rebuild one specific item
python 05_build_lessons.py --show-lessons   # view current active lessons
```
For each item with enough new corrections, calls Claude to synthesize a
concise lesson. Lessons are versioned and stored in the DB. The next time
`02_process_bridges.py` runs, lessons are automatically injected into prompts.

### Monitoring
```bash
python status.py            # overview of pipeline state
python status.py --lessons  # show all active lesson text
python status.py --flagged  # show inspector-flagged items needing attention
python status.py --errors   # show bridges that failed processing
```

---

## Full Workflow

```
CYCLE 1 (first batch)
─────────────────────
python 01_init_db.py                     # load BrM data
python 02_process_bridges.py --limit 50  # test on 50 bridges
python 03_export_csv.py                  # export to Google Sheets
↓ Inspectors review in AppSheet ↓
python 04_import_corrections.py --csv export.csv
python 05_build_lessons.py
python status.py --lessons               # review what the AI learned

CYCLE 2 (improved)
──────────────────
python 02_process_bridges.py --limit 100 # AI now uses lessons from cycle 1
python 03_export_csv.py
↓ Inspectors review (fewer corrections needed) ↓
python 04_import_corrections.py --csv export.csv
python 05_build_lessons.py               # lessons refined with more data

CYCLE N (steady state)
────────────────────────
python 02_process_bridges.py             # process remaining bridges
python 03_export_csv.py
↓ Light review pass ↓
python 04_import_corrections.py --csv export.csv
```

---

## File Structure (complete)

```
snbi_pipeline/
  config.py
  requirements.txt
  01_init_db.py              ← Phase 1: Load BrM data
  02_process_bridges.py      ← Phase 2: PDF extraction (uses lessons)
  03_export_csv.py           ← Phase 3: Export to Google Sheets
  04_import_corrections.py   ← Phase 4: Import inspector feedback
  05_build_lessons.py        ← Phase 5: Distil lessons from feedback
  status.py                  ← Monitor pipeline at any time
  snbi_evidence.db           ← Generated SQLite database
  lib/
    __init__.py
    snbi_items.py            ← Item definitions & page routing
    db.py                    ← All DB operations (incl. corrections/lessons)
    brm_loader.py            ← BrM Excel → DB
    pdf_extractor.py         ← PDF page rendering
    claude_api.py            ← API calls + lesson injection
    results_merger.py        ← API output → DB
```

---

## Database Tables

| Table | Purpose |
|-------|---------|
| `bridges` | One row per bridge — status, paths, metadata |
| `evidence` | One row per bridge×item×feature — all pre-filled + inspector values |
| `corrections` | One row per inspector correction — raw feedback |
| `lessons` | One row per item per version — distilled from corrections |
| `import_batches` | Audit log of each Phase 4 import run |
| `processing_log` | Detailed log of Phase 2 processing |

