"""
db.py — SQLite database setup and helpers
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path):
    """Create all tables if they don't already exist."""
    conn = get_conn(db_path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bridges (
        bridge_id           TEXT PRIMARY KEY,
        bridge_name         TEXT,
        facility_carried    TEXT,
        feature_intersected TEXT,
        feature_category    TEXT,   -- HIGHWAY | RAILROAD | WATERWAY | UNKNOWN
        county              TEXT,
        year_built          TEXT,
        struct_type         TEXT,
        has_vc_doc          INTEGER DEFAULT 0,
        plan_pdf_path       TEXT,
        vc_pdf_paths        TEXT,   -- JSON array of paths
        processing_status   TEXT DEFAULT 'PENDING',
        -- PENDING | BRM_DONE | PLANS_DONE | COMPLETE | ERROR
        created_at          TEXT DEFAULT (datetime('now')),
        updated_at          TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS evidence (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_id           TEXT NOT NULL,
        item_id             TEXT NOT NULL,   -- e.g. B.G.01
        feature_id          TEXT NOT NULL DEFAULT 'PRIMARY',
        -- PRIMARY | WORK:1982 | H01 | W01 | R01 | P01 etc.
        item_name           TEXT,
        brm_value           TEXT,
        brm_source_col      TEXT,
        plan_value          TEXT,
        plan_confidence     TEXT DEFAULT 'PENDING',
        -- HIGH | APPROX | FIELD_REQ | NA | PENDING
        plan_reasoning      TEXT,
        plan_source_pages   TEXT,
        auto_questions      TEXT,   -- pipeline-generated questions
        user_determination  TEXT,   -- inspector fills this
        user_notes          TEXT,
        status              TEXT DEFAULT 'PENDING',
        -- PENDING | REVIEWED | FLAGGED | APPROVED
        reviewed_by         TEXT,
        reviewed_date       TEXT,
        created_at          TEXT DEFAULT (datetime('now')),
        updated_at          TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (bridge_id) REFERENCES bridges(bridge_id),
        UNIQUE(bridge_id, item_id, feature_id)
    );

    CREATE TABLE IF NOT EXISTS processing_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_id   TEXT,
        phase       TEXT,    -- BRM | PLANS | CLEARANCE | EXPORT
        status      TEXT,    -- SUCCESS | ERROR | SKIPPED
        message     TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_evidence_bridge  ON evidence(bridge_id);
    CREATE INDEX IF NOT EXISTS idx_evidence_status  ON evidence(status);
    CREATE INDEX IF NOT EXISTS idx_evidence_item    ON evidence(item_id);
    CREATE INDEX IF NOT EXISTS idx_log_bridge       ON processing_log(bridge_id);
    """)
    conn.commit()
    conn.close()
    print(f"Database initialised at {db_path}")


# ── Bridge helpers ─────────────────────────────────────────────────────────

def upsert_bridge(conn, row: dict):
    conn.execute("""
        INSERT INTO bridges
            (bridge_id, bridge_name, facility_carried, feature_intersected,
             feature_category, county, year_built, struct_type,
             has_vc_doc, plan_pdf_path, vc_pdf_paths, processing_status)
        VALUES
            (:bridge_id,:bridge_name,:facility_carried,:feature_intersected,
             :feature_category,:county,:year_built,:struct_type,
             :has_vc_doc,:plan_pdf_path,:vc_pdf_paths,:processing_status)
        ON CONFLICT(bridge_id) DO UPDATE SET
            bridge_name         = excluded.bridge_name,
            facility_carried    = excluded.facility_carried,
            feature_intersected = excluded.feature_intersected,
            feature_category    = excluded.feature_category,
            county              = excluded.county,
            year_built          = excluded.year_built,
            struct_type         = excluded.struct_type,
            has_vc_doc          = excluded.has_vc_doc,
            plan_pdf_path       = excluded.plan_pdf_path,
            vc_pdf_paths        = excluded.vc_pdf_paths,
            updated_at          = datetime('now')
    """, row)


def get_bridge(conn, bridge_id):
    return conn.execute(
        "SELECT * FROM bridges WHERE bridge_id = ?", (bridge_id,)
    ).fetchone()


def get_bridges_by_status(conn, status):
    return conn.execute(
        "SELECT * FROM bridges WHERE processing_status = ?", (status,)
    ).fetchall()


def set_bridge_status(conn, bridge_id, status):
    conn.execute(
        "UPDATE bridges SET processing_status=?, updated_at=datetime('now') WHERE bridge_id=?",
        (status, bridge_id)
    )


# ── Evidence helpers ───────────────────────────────────────────────────────

def upsert_evidence(conn, row: dict):
    """Insert or update an evidence row. row must have bridge_id, item_id, feature_id."""
    conn.execute("""
        INSERT INTO evidence
            (bridge_id, item_id, feature_id, item_name,
             brm_value, brm_source_col,
             plan_value, plan_confidence, plan_reasoning, plan_source_pages,
             auto_questions, status)
        VALUES
            (:bridge_id,:item_id,:feature_id,:item_name,
             :brm_value,:brm_source_col,
             :plan_value,:plan_confidence,:plan_reasoning,:plan_source_pages,
             :auto_questions,:status)
        ON CONFLICT(bridge_id, item_id, feature_id) DO UPDATE SET
            item_name           = excluded.item_name,
            brm_value           = COALESCE(excluded.brm_value, brm_value),
            brm_source_col      = COALESCE(excluded.brm_source_col, brm_source_col),
            plan_value          = COALESCE(excluded.plan_value, plan_value),
            plan_confidence     = COALESCE(excluded.plan_confidence, plan_confidence),
            plan_reasoning      = COALESCE(excluded.plan_reasoning, plan_reasoning),
            plan_source_pages   = COALESCE(excluded.plan_source_pages, plan_source_pages),
            auto_questions      = COALESCE(excluded.auto_questions, auto_questions),
            updated_at          = datetime('now')
    """, {
        "bridge_id":        row.get("bridge_id"),
        "item_id":          row.get("item_id"),
        "feature_id":       row.get("feature_id", "PRIMARY"),
        "item_name":        row.get("item_name"),
        "brm_value":        row.get("brm_value"),
        "brm_source_col":   row.get("brm_source_col"),
        "plan_value":       row.get("plan_value"),
        "plan_confidence":  row.get("plan_confidence", "PENDING"),
        "plan_reasoning":   row.get("plan_reasoning"),
        "plan_source_pages":row.get("plan_source_pages"),
        "auto_questions":   row.get("auto_questions"),
        "status":           row.get("status", "PENDING"),
    })


def get_evidence_for_bridge(conn, bridge_id):
    return conn.execute(
        "SELECT * FROM evidence WHERE bridge_id = ? ORDER BY item_id, feature_id",
        (bridge_id,)
    ).fetchall()


def get_all_evidence(conn):
    return conn.execute(
        "SELECT * FROM evidence ORDER BY bridge_id, item_id, feature_id"
    ).fetchall()


def get_below_features(conn, bridge_id, type_prefix=None):
    """
    Return feature_ids where B.F.02 = 'B' (below) for a bridge.
    type_prefix: if given, restrict to feature IDs starting with that character
                 (e.g. 'H' for highway, 'R' for railroad, 'W' for waterway).
    Checks both brm_value and plan_value so plan overrides are respected.
    """
    rows = conn.execute(
        """SELECT DISTINCT feature_id FROM evidence
           WHERE bridge_id=? AND item_id='B.F.02'
             AND (brm_value='B' OR plan_value='B')""",
        (bridge_id,)
    ).fetchall()
    fids = [r[0] for r in rows]
    if type_prefix:
        fids = [f for f in fids if f.startswith(type_prefix)]
    return fids


# ── Logging ────────────────────────────────────────────────────────────────

def log(conn, bridge_id, phase, status, message=""):
    conn.execute(
        "INSERT INTO processing_log (bridge_id, phase, status, message) VALUES (?,?,?,?)",
        (bridge_id, phase, status, str(message)[:2000])
    )


# ── Stats ──────────────────────────────────────────────────────────────────

def print_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM bridges").fetchone()[0]
    by_status = conn.execute(
        "SELECT processing_status, COUNT(*) FROM bridges GROUP BY processing_status"
    ).fetchall()
    ev_total = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    ev_pending = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE plan_confidence='PENDING'"
    ).fetchone()[0]
    ev_field = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE plan_confidence='FIELD_REQ'"
    ).fetchone()[0]
    print(f"\n── Database stats ───────────────────────────────────")
    print(f"  Bridges total: {total}")
    for row in by_status:
        print(f"    {row[0]:20s}: {row[1]}")
    print(f"  Evidence rows: {ev_total}")
    print(f"    PENDING (not yet extracted): {ev_pending}")
    print(f"    FIELD_REQ (needs inspector): {ev_field}")
    print(f"────────────────────────────────────────────────────\n")


def migrate_db(db_path):
    """Add feedback tables and new columns to existing database. Safe to re-run."""
    conn = get_conn(db_path)
    # Add lat/lon columns if not present (SQLite doesn't support IF NOT EXISTS on columns)
    for sql in [
        "ALTER TABLE bridges ADD COLUMN lat TEXT",
        "ALTER TABLE bridges ADD COLUMN lon TEXT",
        "ALTER TABLE evidence ADD COLUMN needs_field INTEGER DEFAULT 0",
        "ALTER TABLE evidence ADD COLUMN gis_value TEXT",
        "ALTER TABLE evidence ADD COLUMN gis_source TEXT",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass  # column already exists
    # Migrate existing APPROX plan_values that came from GIS/automated sources
    # (Phase 9 TransGIS names, Phase 10 USCG, old Phase 8 movable-bridge 999.9).
    # Rows with long Claude-written reasoning (e.g. "NBI Item 116...") are left in plan_value.
    conn.execute("""
        UPDATE evidence
           SET gis_value      = plan_value,
               gis_source     = COALESCE(plan_reasoning, 'GIS pre-fill'),
               plan_value     = NULL,
               plan_confidence = 'PENDING',
               plan_reasoning = NULL
         WHERE plan_confidence = 'APPROX'
           AND plan_value IS NOT NULL
           AND gis_value IS NULL
           AND (
               plan_reasoning IS NULL
               OR plan_reasoning LIKE '%USCG%'
               OR plan_reasoning LIKE '%ODOT TransGIS%'
               OR (plan_reasoning LIKE '%Movable%' AND plan_reasoning LIKE '%clearance%')
           )
    """)
    # Migrate GIS-sourced B.RR.01 rows written to brm_value before the fill_rr01 column fix.
    conn.execute("""
        UPDATE evidence
           SET gis_value      = brm_value,
               gis_source     = brm_source_col,
               brm_value      = NULL,
               brm_source_col = NULL
         WHERE item_id = 'B.RR.01'
           AND brm_source_col LIKE '%TransGIS%'
           AND gis_value IS NULL
    """)
    conn.commit()
    # Migrate existing APPROX plan_values that came from GIS/automated sources
    # (Phase 9 TransGIS names, Phase 10 USCG, old Phase 8 movable-bridge 999.9).
    # Rows with long Claude-written reasoning (e.g. "NBI Item 116...") are left in plan_value.
    conn.execute("""
        UPDATE evidence
           SET gis_value      = plan_value,
               gis_source     = COALESCE(plan_reasoning, 'GIS pre-fill'),
               plan_value     = NULL,
               plan_confidence = 'PENDING',
               plan_reasoning = NULL
         WHERE plan_confidence = 'APPROX'
           AND plan_value IS NOT NULL
           AND gis_value IS NULL
           AND (
               plan_reasoning IS NULL
               OR plan_reasoning LIKE '%USCG%'
               OR plan_reasoning LIKE '%ODOT TransGIS%'
               OR (plan_reasoning LIKE '%Movable%' AND plan_reasoning LIKE '%clearance%')
           )
    """)
    # Migrate GIS-sourced B.RR.01 rows written to brm_value before the fill_rr01 column fix.
    conn.execute("""
        UPDATE evidence
           SET gis_value      = brm_value,
               gis_source     = brm_source_col,
               brm_value      = NULL,
               brm_source_col = NULL
         WHERE item_id = 'B.RR.01'
           AND brm_source_col LIKE '%TransGIS%'
           AND gis_value IS NULL
    """)
    conn.commit()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS corrections (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_id           TEXT NOT NULL,
        item_id             TEXT NOT NULL,
        feature_id          TEXT NOT NULL DEFAULT 'PRIMARY',
        -- What the AI said
        ai_value            TEXT,
        ai_confidence       TEXT,
        ai_reasoning        TEXT,
        ai_source_pages     TEXT,
        -- What the inspector determined
        field_value         TEXT NOT NULL,
        field_notes         TEXT,
        reviewer            TEXT,
        reviewed_date       TEXT,
        -- Correction metadata
        correction_type     TEXT,
        -- VALUE_WRONG      : AI value was incorrect
        -- CONFIDENCE_WRONG : Value OK but confidence wrong
        -- REASONING_WRONG  : Value OK but reasoning/source wrong
        -- NA_WRONG         : AI marked NA but item applies (or vice versa)
        -- CONFIRMED        : Inspector confirmed AI value (positive signal)
        import_batch        TEXT,   -- datestamp of import run
        used_in_lesson      INTEGER DEFAULT 0,  -- 1 once distilled into a lesson
        created_at          TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS lessons (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id             TEXT NOT NULL,
        lesson_text         TEXT NOT NULL,
        -- Concise guidance text injected into future prompts
        example_json        TEXT,
        -- JSON snippet showing a good before/after example
        correction_count    INTEGER DEFAULT 0,
        -- How many corrections this lesson is based on
        confirmed_count     INTEGER DEFAULT 0,
        -- How many confirmations support this lesson
        confidence_score    REAL DEFAULT 0.0,
        -- 0.0-1.0, higher = more reliable lesson
        version             INTEGER DEFAULT 1,
        active              INTEGER DEFAULT 1,  -- 0 = superseded
        created_at          TEXT DEFAULT (datetime('now')),
        updated_at          TEXT DEFAULT (datetime('now')),
        UNIQUE(item_id, version)
    );

    CREATE TABLE IF NOT EXISTS import_batches (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id        TEXT UNIQUE,    -- datestamp e.g. 2025-04-23
        csv_path        TEXT,
        rows_imported   INTEGER DEFAULT 0,
        corrections     INTEGER DEFAULT 0,
        confirmations   INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_corrections_item    ON corrections(item_id);
    CREATE INDEX IF NOT EXISTS idx_corrections_bridge  ON corrections(bridge_id);
    CREATE INDEX IF NOT EXISTS idx_corrections_lesson  ON corrections(used_in_lesson);
    CREATE INDEX IF NOT EXISTS idx_lessons_item        ON lessons(item_id);
    """)

    # Screenshots table (safe to add after initial migration)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS screenshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        evidence_id INTEGER NOT NULL,
        filename    TEXT NOT NULL,
        caption     TEXT,
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (evidence_id) REFERENCES evidence(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_screenshots_evidence ON screenshots(evidence_id);
    """)

    # Bridge log table — raw entry text from brlog.pdf, populated by 07_import_bridge_log.py
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bridge_log (
        bridge_id   TEXT PRIMARY KEY,
        raw_entry   TEXT NOT NULL,
        imported_at TEXT DEFAULT (datetime('now'))
    );
    """)

    # Features table — one row per feature (H01, W01, R01, etc.) with SNBI-format UUID
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS features (
        id           TEXT PRIMARY KEY,
        bridge_id    TEXT NOT NULL,
        designation  TEXT NOT NULL,
        feature_type TEXT NOT NULL,
        location     TEXT,
        created_at   TEXT DEFAULT (datetime('now')),
        UNIQUE(bridge_id, designation),
        FOREIGN KEY (bridge_id) REFERENCES bridges(bridge_id)
    );
    CREATE INDEX IF NOT EXISTS idx_features_bridge ON features(bridge_id);
    """)

    # Populate features from existing evidence rows (migration for existing DBs).
    # One bulk query to find gaps, then batch-insert — avoids N+1 queries.
    import uuid as _uuid
    already_set = set(
        (r["bridge_id"], r["designation"])
        for r in conn.execute("SELECT bridge_id, designation FROM features").fetchall()
    )
    candidates = conn.execute("""
        SELECT DISTINCT e.bridge_id, e.feature_id,
               MAX(CASE WHEN e.item_id='B.F.02' THEN COALESCE(e.plan_value, e.brm_value) END) AS location
        FROM evidence e
        WHERE e.feature_id != 'PRIMARY' AND e.feature_id NOT LIKE 'WORK:%'
        GROUP BY e.bridge_id, e.feature_id
    """).fetchall()
    for row in candidates:
        bid, fid = row["bridge_id"], row["feature_id"]
        if (bid, fid) in already_set:
            continue
        location = (row["location"] or "").strip() or None
        new_uuid = _uuid.uuid4().hex[:8].upper()
        conn.execute(
            "INSERT OR IGNORE INTO features (id, bridge_id, designation, feature_type, location) VALUES (?,?,?,?,?)",
            (new_uuid, bid, fid, fid[0].upper(), location)
        )

    # Validation results — populated by the SNBI Validation tab / batch export
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS validation_results (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_id   TEXT NOT NULL,
        run_at      TEXT DEFAULT (datetime('now')),
        severity    TEXT NOT NULL,
        rule_name   TEXT NOT NULL,
        snbi_id     TEXT,
        feature_id  TEXT,
        description TEXT NOT NULL,
        explanation TEXT,
        FOREIGN KEY (bridge_id) REFERENCES bridges(bridge_id)
    );
    CREATE INDEX IF NOT EXISTS idx_valresults_bridge ON validation_results(bridge_id);
    """)

    conn.commit()
    conn.close()
    print(f"Feedback tables added/verified at {db_path}")


# ── Correction helpers ─────────────────────────────────────────────────────

def insert_correction(conn, row: dict):
    conn.execute("""
        INSERT OR IGNORE INTO corrections
            (bridge_id, item_id, feature_id,
             ai_value, ai_confidence, ai_reasoning, ai_source_pages,
             field_value, field_notes, reviewer, reviewed_date,
             correction_type, import_batch)
        VALUES
            (:bridge_id,:item_id,:feature_id,
             :ai_value,:ai_confidence,:ai_reasoning,:ai_source_pages,
             :field_value,:field_notes,:reviewer,:reviewed_date,
             :correction_type,:import_batch)
    """, row)


def get_corrections_for_item(conn, item_id, unused_only=True):
    q = "SELECT * FROM corrections WHERE item_id=?"
    params = [item_id]
    if unused_only:
        q += " AND used_in_lesson=0"
    q += " ORDER BY created_at DESC"
    return conn.execute(q, params).fetchall()


def get_all_corrections(conn):
    return conn.execute(
        "SELECT * FROM corrections ORDER BY item_id, created_at DESC"
    ).fetchall()


def mark_corrections_used(conn, correction_ids: list):
    if not correction_ids:
        return
    placeholders = ",".join("?" * len(correction_ids))
    conn.execute(
        f"UPDATE corrections SET used_in_lesson=1 WHERE id IN ({placeholders})",
        correction_ids
    )


# ── Lesson helpers ─────────────────────────────────────────────────────────

def upsert_lesson(conn, item_id: str, lesson_text: str, example_json: str,
                  correction_count: int, confirmed_count: int, confidence_score: float):
    # Deactivate previous version
    conn.execute(
        "UPDATE lessons SET active=0 WHERE item_id=? AND active=1",
        (item_id,)
    )
    # Get next version number
    row = conn.execute(
        "SELECT MAX(version) FROM lessons WHERE item_id=?", (item_id,)
    ).fetchone()
    next_version = (row[0] or 0) + 1

    conn.execute("""
        INSERT INTO lessons
            (item_id, lesson_text, example_json, correction_count,
             confirmed_count, confidence_score, version, active)
        VALUES (?,?,?,?,?,?,?,1)
    """, (item_id, lesson_text, example_json,
          correction_count, confirmed_count, confidence_score, next_version))


def get_active_lessons(conn):
    """Returns dict: item_id → lesson row"""
    rows = conn.execute(
        "SELECT * FROM lessons WHERE active=1 ORDER BY item_id"
    ).fetchall()
    return {r["item_id"]: dict(r) for r in rows}


def get_lesson_for_item(conn, item_id):
    return conn.execute(
        "SELECT * FROM lessons WHERE item_id=? AND active=1", (item_id,)
    ).fetchone()


# ── Screenshot helpers ─────────────────────────────────────────────────────

def add_screenshot(conn, evidence_id: int, filename: str, caption: str = None) -> int:
    cur = conn.execute(
        "INSERT INTO screenshots (evidence_id, filename, caption) VALUES (?,?,?)",
        (evidence_id, filename, caption)
    )
    return cur.lastrowid


def get_screenshots(conn, evidence_id: int):
    return conn.execute(
        "SELECT id, filename, caption, created_at FROM screenshots WHERE evidence_id=? ORDER BY id",
        (evidence_id,)
    ).fetchall()


def delete_screenshot(conn, shot_id: int):
    conn.execute("DELETE FROM screenshots WHERE id=?", (shot_id,))


def get_lesson_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0]
    unused = conn.execute(
        "SELECT COUNT(*) FROM corrections WHERE used_in_lesson=0"
    ).fetchone()[0]
    lessons = conn.execute("SELECT COUNT(*) FROM lessons WHERE active=1").fetchone()[0]
    by_item = conn.execute("""
        SELECT item_id, COUNT(*) as n,
               SUM(CASE WHEN correction_type='CONFIRMED' THEN 1 ELSE 0 END) as confirmed,
               SUM(CASE WHEN correction_type!='CONFIRMED' THEN 1 ELSE 0 END) as corrected
        FROM corrections GROUP BY item_id ORDER BY n DESC
    """).fetchall()
    print(f"\n── Feedback stats ──────────────────────────────────")
    print(f"  Total feedback rows : {total}")
    print(f"  Unused (new)        : {unused}")
    print(f"  Active lessons      : {lessons}")
    print(f"  Top corrected items:")
    for r in by_item[:10]:
        print(f"    {r['item_id']:12s}  corrections={r['corrected']:3d}  confirmations={r['confirmed']:3d}")
    print(f"────────────────────────────────────────────────────\n")
