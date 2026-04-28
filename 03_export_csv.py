#!/usr/bin/env python3
"""
03_export_csv.py — Phase 3: Export evidence table to CSVs for Google Sheets.

Generates two CSV files:
  1. snbi_evidence.csv     — main evidence table (import to Google Sheets)
  2. missing_bridges.csv   — bridge IDs not yet in your AppSheet bridge table

Usage:
    python 03_export_csv.py
    python 03_export_csv.py --output-dir "C:/Users/rcase/Google Drive/SNBI"
"""
import sys
import os
import csv
import argparse
import sqlite3
sys.path.insert(0, os.path.dirname(__file__))

import config
from lib.db import get_conn, get_all_evidence, print_stats
from lib.snbi_items import ITEM_BY_ID, PRIMARY, WORK, FEATURE


# ── Column definitions for Google Sheets ──────────────────────────────────
# These become the column headers in the imported sheet.
# AppSheet will use these to build its data model.

EVIDENCE_COLUMNS = [
    # Key (AppSheet will use this as the unique row identifier)
    "evidence_id",          # {bridge_id}-{item_id}-{feature_id}

    # Bridge link (ref to existing bridge table)
    "bridge_id",
    "bridge_name",
    "county",
    "feature_category",     # HIGHWAY | RAILROAD | WATERWAY

    # SNBI item info
    "item_id",              # e.g. B.G.01
    "item_name",            # e.g. "NBIS Bridge Length"
    "item_table",           # PRIMARY | WORK | FEATURE
    "feature_id",           # PRIMARY | WORK:1982 | H01 | W01 etc.
    "item_notes",           # Guidance text for the inspector

    # Pre-filled values
    "brm_value",            # Value from BrM export
    "plan_value",           # Value from plan PDF (Claude extraction)
    "plan_confidence",      # HIGH | APPROX | FIELD_REQ | NA | PENDING
    "plan_reasoning",       # How plan value was derived
    "plan_source_pages",    # Which drawing/page it came from
    "auto_questions",       # Pipeline-generated questions

    # Inspector fills these in AppSheet
    "field_value",          # Final determined value (blank = not reviewed)
    "field_notes",          # Inspector notes / corrections
    "status",               # PENDING | REVIEWED | FLAGGED | APPROVED

    # Review tracking
    "reviewed_by",
    "reviewed_date",
]


def build_evidence_row(ev_row, bridge_lookup):
    """Build one output CSV row from a DB evidence row."""
    bid   = ev_row["bridge_id"]
    iid   = ev_row["item_id"]
    fid   = ev_row["feature_id"] or "PRIMARY"

    bridge = bridge_lookup.get(bid, {})
    item   = ITEM_BY_ID.get(iid, {})

    evidence_id = f"{bid}-{iid}-{fid}".replace(" ", "_")

    return {
        "evidence_id":      evidence_id,
        "bridge_id":        bid,
        "bridge_name":      bridge.get("bridge_name", ""),
        "county":           bridge.get("county", ""),
        "feature_category": bridge.get("feature_category", ""),
        "item_id":          iid,
        "item_name":        ev_row["item_name"] or item.get("name", ""),
        "item_table":       item.get("table", ""),
        "feature_id":       fid,
        "item_notes":       item.get("notes", ""),
        "brm_value":        ev_row["brm_value"] or "",
        "plan_value":       ev_row["plan_value"] or "",
        "plan_confidence":  ev_row["plan_confidence"] or "PENDING",
        "plan_reasoning":   ev_row["plan_reasoning"] or "",
        "plan_source_pages":ev_row["plan_source_pages"] or "",
        "auto_questions":   ev_row["auto_questions"] or "",
        "field_value":      ev_row["user_determination"] or "",
        "field_notes":      ev_row["user_notes"] or "",
        "status":           ev_row["status"] or "PENDING",
        "reviewed_by":      ev_row["reviewed_by"] or "",
        "reviewed_date":    ev_row["reviewed_date"] or "",
    }


def export_csv(conn, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    evidence_path    = os.path.join(output_dir, "snbi_evidence.csv")
    missing_path     = os.path.join(output_dir, "missing_bridges.csv")
    summary_path     = os.path.join(output_dir, "snbi_summary.csv")

    # Load bridge records for lookup
    bridges = conn.execute("SELECT * FROM bridges").fetchall()
    bridge_lookup = {b["bridge_id"]: dict(b) for b in bridges}

    # Load all evidence
    evidence = get_all_evidence(conn)
    print(f"  Exporting {len(evidence)} evidence rows...")

    # ── Write evidence CSV ─────────────────────────────────────
    with open(evidence_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVIDENCE_COLUMNS)
        writer.writeheader()
        for ev in evidence:
            row = build_evidence_row(ev, bridge_lookup)
            writer.writerow(row)
    print(f"  Evidence CSV: {evidence_path}")

    # ── Write missing bridges CSV ──────────────────────────────
    # These bridge IDs need to be added to the AppSheet bridge table
    # before importing the evidence CSV (so refs don't break)
    all_bridge_ids = sorted(bridge_lookup.keys())
    with open(missing_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "bridge_id","bridge_name","facility_carried",
            "feature_intersected","county","year_built","feature_category"
        ])
        writer.writeheader()
        for bid in all_bridge_ids:
            b = bridge_lookup[bid]
            writer.writerow({
                "bridge_id":          bid,
                "bridge_name":        b.get("bridge_name",""),
                "facility_carried":   b.get("facility_carried",""),
                "feature_intersected":b.get("feature_intersected",""),
                "county":             b.get("county",""),
                "year_built":         b.get("year_built",""),
                "feature_category":   b.get("feature_category",""),
            })
    print(f"  Missing bridges CSV: {missing_path}")

    # ── Write summary CSV (one row per bridge) ─────────────────
    summary_rows = _build_summary(conn, bridge_lookup)
    summary_cols = [
        "bridge_id","bridge_name","county","feature_category","processing_status",
        "total_items","pending","high_confidence","approx","field_req","reviewed"
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_cols)
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"  Summary CSV: {summary_path}")

    return evidence_path, missing_path, summary_path


def _build_summary(conn, bridge_lookup):
    rows = []
    result = conn.execute("""
        SELECT
            bridge_id,
            COUNT(*) as total,
            SUM(CASE WHEN plan_confidence='PENDING' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN plan_confidence='HIGH'    THEN 1 ELSE 0 END) as high,
            SUM(CASE WHEN plan_confidence='APPROX'  THEN 1 ELSE 0 END) as approx,
            SUM(CASE WHEN plan_confidence='FIELD_REQ' THEN 1 ELSE 0 END) as field_req,
            SUM(CASE WHEN status='REVIEWED' OR status='APPROVED' THEN 1 ELSE 0 END) as reviewed
        FROM evidence
        GROUP BY bridge_id
        ORDER BY bridge_id
    """).fetchall()

    for r in result:
        bid = r["bridge_id"]
        b   = bridge_lookup.get(bid, {})
        rows.append({
            "bridge_id":         bid,
            "bridge_name":       b.get("bridge_name",""),
            "county":            b.get("county",""),
            "feature_category":  b.get("feature_category",""),
            "processing_status": b.get("processing_status",""),
            "total_items":       r["total"],
            "pending":           r["pending"],
            "high_confidence":   r["high"],
            "approx":            r["approx"],
            "field_req":         r["field_req"],
            "reviewed":          r["reviewed"],
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=None,
                        help="Output directory for CSV files (default: same as config.REVIEW_EXCEL_PATH dir)")
    args = parser.parse_args()

    print("=" * 60)
    print("  SNBI Pipeline — Phase 3: Export to CSV for Google Sheets")
    print("=" * 60)

    if not os.path.exists(config.DB_PATH):
        print(f"ERROR: Database not found: {config.DB_PATH}")
        print("  Run 01_init_db.py first.")
        sys.exit(1)

    output_dir = args.output_dir or os.path.dirname(
        os.path.abspath(config.REVIEW_EXCEL_PATH)
    )

    conn = get_conn(config.DB_PATH)
    print_stats(conn)

    ev_path, miss_path, sum_path = export_csv(conn, output_dir)
    conn.close()

    print(f"\n{'='*60}")
    print("  Export complete.")
    print(f"\n  Files written to: {output_dir}")
    print(f"    snbi_evidence.csv    → import this as a new Google Sheet tab")
    print(f"    missing_bridges.csv  → add these to your AppSheet bridge table first")
    print(f"    snbi_summary.csv     → overview of processing status per bridge")
    print()
    print("  IMPORT STEPS:")
    print("  1. Open your Google Sheet")
    print("  2. Create a new tab called 'snbi_evidence'")
    print("  3. File → Import → Upload snbi_evidence.csv → 'Replace current sheet'")
    print("  4. In AppSheet, add the new sheet as a table")
    print("  5. Set 'evidence_id' as the key column")
    print("  6. Create a Ref from 'bridge_id' → your existing bridge table")


if __name__ == "__main__":
    main()
