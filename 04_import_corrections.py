#!/usr/bin/env python3
"""
04_import_corrections.py — Phase 4: Import inspector feedback from Google Sheets.

After inspectors review evidence rows in AppSheet/Google Sheets, export the
snbi_evidence sheet as CSV and run this script to pull their corrections and
confirmations into the database.

The script compares field_value vs plan_value for every reviewed row and
classifies each as a CORRECTION or CONFIRMATION. These feed into Phase 5
(lesson building).

Usage:
    python 04_import_corrections.py --csv "path/to/snbi_evidence_export.csv"
    python 04_import_corrections.py --csv snbi_evidence_export.csv --dry-run
"""
import sys
import os
import csv
import argparse
from datetime import datetime
sys.path.insert(0, os.path.dirname(__file__))

import config
from lib.db import (
    get_conn, migrate_db, insert_correction, get_lesson_stats, log
)


# ── Helpers ────────────────────────────────────────────────────────────────

def values_differ(ai_val, field_val):
    """Return True if the inspector's value meaningfully differs from AI's."""
    if not field_val or not field_val.strip():
        return False  # inspector left blank — not a correction
    if not ai_val or not ai_val.strip():
        return True   # AI had nothing, inspector provided something

    a = str(ai_val).strip().lower()
    f = str(field_val).strip().lower()

    if a == f:
        return False

    # Numeric near-match (within 0.2 ft) — treat as confirmation
    try:
        fa, ff = float(a), float(f)
        if abs(fa - ff) <= 0.2:
            return False
    except ValueError:
        pass

    return True


def classify_correction(row):
    """
    Given a CSV row, determine what kind of feedback this is.
    Returns correction_type string or None if not reviewable.
    """
    status = (row.get("status") or "").strip().upper()
    if status not in ("REVIEWED", "FLAGGED", "APPROVED"):
        return None  # Not yet reviewed by inspector

    field_val = (row.get("field_value") or "").strip()
    ai_val    = (row.get("plan_value")  or "").strip()
    confidence= (row.get("plan_confidence") or "").strip()

    if not field_val:
        return None  # Inspector reviewed but left field_value blank — skip

    if status == "FLAGGED":
        return "VALUE_WRONG"  # Flagged always counts as a correction

    if not values_differ(ai_val, field_val):
        return "CONFIRMED"

    # It differs — try to classify why
    if confidence == "NA" and field_val.upper() not in ("NA", "N/A", "NOT APPLICABLE"):
        return "NA_WRONG"
    if ai_val and not values_differ(ai_val, field_val):
        return "CONFIDENCE_WRONG"
    return "VALUE_WRONG"


def import_csv(conn, csv_path, batch_id, dry_run=False):
    corrections  = 0
    confirmations = 0
    skipped      = 0
    errors       = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)

    print(f"  Rows in CSV: {len(rows)}")

    for row in rows:
        bridge_id  = (row.get("bridge_id")   or "").strip()
        item_id    = (row.get("item_id")      or "").strip()
        feature_id = (row.get("feature_id")   or "PRIMARY").strip()
        field_val  = (row.get("field_value")  or "").strip()
        field_notes= (row.get("field_notes")  or "").strip()
        reviewer   = (row.get("reviewed_by")  or "").strip()
        rev_date   = (row.get("reviewed_date")or "").strip()

        if not bridge_id or not item_id:
            skipped += 1
            continue

        correction_type = classify_correction(row)
        if not correction_type:
            skipped += 1
            continue

        if not dry_run:
            try:
                insert_correction(conn, {
                    "bridge_id":       bridge_id,
                    "item_id":         item_id,
                    "feature_id":      feature_id,
                    "ai_value":        row.get("plan_value",""),
                    "ai_confidence":   row.get("plan_confidence",""),
                    "ai_reasoning":    row.get("plan_reasoning",""),
                    "ai_source_pages": row.get("plan_source_pages",""),
                    "field_value":     field_val,
                    "field_notes":     field_notes,
                    "reviewer":        reviewer,
                    "reviewed_date":   rev_date,
                    "correction_type": correction_type,
                    "import_batch":    batch_id,
                })

                # Also update the evidence table with the inspector's determination
                conn.execute("""
                    UPDATE evidence SET
                        user_determination = ?,
                        user_notes         = ?,
                        reviewed_by        = ?,
                        reviewed_date      = ?,
                        status             = ?,
                        updated_at         = datetime('now')
                    WHERE bridge_id=? AND item_id=? AND feature_id=?
                """, (
                    field_val, field_notes, reviewer, rev_date,
                    row.get("status","REVIEWED"),
                    bridge_id, item_id, feature_id
                ))
            except Exception as e:
                errors += 1
                print(f"    ERROR {bridge_id}/{item_id}: {e}")
                continue

        if correction_type == "CONFIRMED":
            confirmations += 1
        else:
            corrections += 1

        if dry_run:
            print(f"  DRY-RUN [{correction_type:16s}] {bridge_id:8s} {item_id:8s} "
                  f"AI={row.get('plan_value','')!r:20s} → FIELD={field_val!r}")

    return corrections, confirmations, skipped, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True,
                        help="Path to exported snbi_evidence CSV from Google Sheets")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be imported without writing to DB")
    args = parser.parse_args()

    print("=" * 60)
    print("  SNBI Pipeline — Phase 4: Import Inspector Corrections")
    print("=" * 60)

    if not os.path.exists(args.csv):
        print(f"ERROR: CSV not found: {args.csv}")
        sys.exit(1)

    if not os.path.exists(config.DB_PATH):
        print(f"ERROR: Database not found: {config.DB_PATH}")
        print("  Run 01_init_db.py first.")
        sys.exit(1)

    conn = get_conn(config.DB_PATH)

    # Ensure feedback tables exist (safe to run on existing DB)
    migrate_db(config.DB_PATH)

    batch_id = datetime.now().strftime("%Y-%m-%d_%H%M")
    print(f"  Import batch: {batch_id}")
    print(f"  Source CSV:   {args.csv}")
    if args.dry_run:
        print("  MODE: DRY RUN — nothing will be written\n")

    corrections, confirmations, skipped, errors = import_csv(
        conn, args.csv, batch_id, dry_run=args.dry_run
    )

    if not args.dry_run:
        # Record the batch
        conn.execute("""
            INSERT OR REPLACE INTO import_batches
                (batch_id, csv_path, rows_imported, corrections, confirmations)
            VALUES (?,?,?,?,?)
        """, (batch_id, args.csv,
              corrections + confirmations, corrections, confirmations))
        conn.commit()

    print(f"\n  Results:")
    print(f"    Corrections   (AI was wrong):  {corrections}")
    print(f"    Confirmations (AI was right):   {confirmations}")
    print(f"    Skipped (not yet reviewed):     {skipped}")
    print(f"    Errors:                         {errors}")

    if not args.dry_run:
        get_lesson_stats(conn)
        print("Next step: run  python 05_build_lessons.py")
    else:
        print("\nRe-run without --dry-run to commit these changes.")

    conn.close()


if __name__ == "__main__":
    main()
