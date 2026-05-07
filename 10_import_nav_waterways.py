#!/usr/bin/env python3
"""
10_import_nav_waterways.py — Pre-fill B.N.01 (Navigable Waterway) from the
USCG 13th Coast Guard District navigability determination list.

Logic:
  1. For each waterway bridge (feature_category = WATERWAY), get the best
     available waterway name: B.F.03 plan_value > B.F.03 brm_value >
     bridges.feature_intersected (in priority order).
  2. Look up the name in the USCG 13th District list (lib/nav_waterways.py).
  3. If found → write B.N.01 plan_value ("Y" or "N") with APPROX confidence,
     but only if plan_value is currently NULL (idempotent; never downgrades HIGH).
  4. If not found → leave PENDING for Phase 2 / inspector review.

Source document:
  Navigability_Determination_for_the_13th_Coast_Guard_District.pdf
  (Exhibit 11-K-1, USCG 13th District — copy in project root)

Waterways not in the list may still be navigable if tidal or used for
interstate commerce. Those cases are left PENDING for Phase 2 and inspectors.

Run order: after Phase 9 (09_discover_features.py), before Phase 2.
Idempotent — safe to re-run; only fills null plan_values.

Usage:
    python 10_import_nav_waterways.py
    python 10_import_nav_waterways.py --dry-run
    python 10_import_nav_waterways.py --bridge 02283
    python 10_import_nav_waterways.py --verbose
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DB_PATH
from lib.db import get_conn
from lib.nav_waterways import lookup_with_reasoning

SOURCE = "USCG 13th District Navigability List (10_import_nav_waterways)"


def get_waterway_bridges(conn, bridge_id_filter=None):
    """
    Return rows for waterway bridges that have a B.N.01/W01 evidence row
    with plan_value still NULL.
    """
    sql = """
        SELECT
            e.id          AS evidence_id,
            e.bridge_id,
            b.feature_intersected,
            f03.plan_value  AS f03_plan,
            f03.brm_value   AS f03_brm
        FROM evidence e
        JOIN bridges b ON b.bridge_id = e.bridge_id
        LEFT JOIN evidence f03
            ON  f03.bridge_id  = e.bridge_id
            AND f03.item_id    = 'B.F.03'
            AND f03.feature_id = 'W01'
        WHERE e.item_id    = 'B.N.01'
          AND e.feature_id = 'W01'
          AND e.plan_value IS NULL
          AND b.feature_category = 'WATERWAY'
    """
    params = []
    if bridge_id_filter:
        sql += " AND e.bridge_id = ?"
        params.append(bridge_id_filter)
    sql += " ORDER BY e.bridge_id"
    return conn.execute(sql, params).fetchall()


def best_name(row) -> str | None:
    """Pick the best waterway name from available sources."""
    return (
        (row["f03_plan"] or "").strip() or
        (row["f03_brm"]  or "").strip() or
        (row["feature_intersected"] or "").strip() or
        None
    )


def run(dry_run: bool, bridge_filter: str | None, verbose: bool):
    conn = get_conn(DB_PATH)

    rows = get_waterway_bridges(conn, bridge_filter)
    print(f"Waterway bridges with B.N.01 pending: {len(rows)}")

    matched = skipped = not_found = 0

    for row in rows:
        bridge_id = row["bridge_id"]
        name = best_name(row)

        if not name:
            if verbose:
                print(f"  {bridge_id}: no waterway name available — skip")
            skipped += 1
            continue

        nav_result, reasoning = lookup_with_reasoning(name, state="OR")

        if nav_result is None:
            if verbose:
                print(f"  {bridge_id}: '{name}' — {reasoning}")
            not_found += 1
            continue

        if not dry_run:
            conn.execute(
                """UPDATE evidence
                      SET plan_value      = ?,
                          plan_confidence = 'APPROX',
                          plan_reasoning  = ?,
                          brm_source_col  = ?,
                          updated_at      = datetime('now')
                    WHERE id = ?""",
                (nav_result, reasoning, SOURCE, row["evidence_id"]),
            )

        label = "[DRY RUN] " if dry_run else ""
        print(f"  {label}{bridge_id}: '{name}' -> B.N.01={nav_result}  ({reasoning})")
        matched += 1

    if not dry_run:
        conn.commit()

    conn.close()
    print(
        f"\nDone - set: {matched}  not found: {not_found}  skipped (no name): {skipped}"
    )
    if dry_run:
        print("(dry run - no changes written)")


def main():
    parser = argparse.ArgumentParser(description="Pre-fill B.N.01 from USCG navigability list")
    parser.add_argument("--dry-run",  action="store_true", help="Print results without writing")
    parser.add_argument("--bridge",   metavar="ID",        help="Process a single bridge ID")
    parser.add_argument("--verbose",  action="store_true", help="Print all bridges including not-found")
    args = parser.parse_args()
    run(args.dry_run, args.bridge, args.verbose)


if __name__ == "__main__":
    main()
