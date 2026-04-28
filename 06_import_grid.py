#!/usr/bin/env python3
"""
06_import_grid.py — Import structural data from BrM Grid Export.

Fills in struct_type on the bridges table and populates BrM values for
geometry and span items that the original Bridge_List_Export didn't contain:
  B.SP.02  Number of Spans        (mainspans / appspans)
  B.SP.04  Span Material          (materialmain / NBI 43B)
  B.SP.06  Span Type              (designmain  / NBI 43A)
  B.G.01   NBIS Bridge Length     (length      / NBI 49)  — fills gaps only
  B.G.02   Total Bridge Length    (length      / NBI 49)
  B.G.05   Bridge Width OtO       (deckwidth   / NBI 52)  — fills gaps only
  B.G.06   Bridge Width CtC       (roadwidth   / NBI 51)
  B.G.07   Left Curb Width        (lftcurbsw   / NBI 50A) — fills gaps only
  B.G.08   Right Curb Width       (rtcurbsw    / NBI 50B) — fills gaps only
  B.G.09   Approach Roadway Width (aroadwidth  / NBI 32)
  B.G.11   Skew                   (skew        / NBI 34)

Also stores lat/long and year reconstructed on the bridges table.

Safe to re-run — uses upsert logic and only overwrites null BrM values
for items already populated by Phase 1.

Usage:
    python 06_import_grid.py
    python 06_import_grid.py --dry-run   # preview without writing
"""
import os, sys, argparse
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH
from lib.db import get_conn, migrate_db

GRID_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "BrM_Grid_Export.xlsx")


def clean(val):
    """Strip whitespace, return None for empty/nan."""
    s = str(val).strip()
    return s if s and s.lower() not in ("nan", "none", "") else None


def upsert_ev(conn, bridge_id, item_id, item_name, value, source_col, fill_gaps_only=False, dry_run=False):
    """Insert or selectively update an evidence row. Returns True if a write occurred."""
    if not value:
        return False

    existing = conn.execute(
        "SELECT id, brm_value FROM evidence WHERE bridge_id=? AND item_id=? AND feature_id='PRIMARY'",
        (bridge_id, item_id)
    ).fetchone()

    if existing:
        if fill_gaps_only and existing["brm_value"]:
            return False  # already has a value — leave it alone
        if not dry_run:
            conn.execute(
                "UPDATE evidence SET brm_value=?, brm_source_col=?, updated_at=datetime('now') WHERE id=?",
                (value, source_col, existing["id"])
            )
        return True
    else:
        if not dry_run:
            conn.execute(
                "INSERT INTO evidence (bridge_id, item_id, feature_id, item_name, brm_value, "
                "brm_source_col, plan_confidence, status) VALUES (?,?,?,?,?,?,'PENDING','PENDING')",
                (bridge_id, item_id, "PRIMARY", item_name, value, source_col)
            )
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()

    print("=" * 60)
    print("  Phase 6: Import BrM Grid Export")
    print("=" * 60)
    if args.dry_run:
        print("  DRY RUN — no changes will be written\n")

    if not os.path.exists(GRID_PATH):
        print(f"ERROR: {GRID_PATH} not found")
        sys.exit(1)

    print(f"Reading {GRID_PATH} ...")
    df = pd.read_excel(GRID_PATH, dtype=str).fillna("")
    df["bridge id"] = df["bridge id"].str.strip()
    print(f"  Grid rows: {len(df)}")

    conn = get_conn(DB_PATH)
    migrate_db(DB_PATH)  # ensures lat/lon columns exist
    our_ids  = set(r[0] for r in conn.execute("SELECT bridge_id FROM bridges").fetchall())
    grid_map = {row["bridge id"]: row for _, row in df.iterrows() if row["bridge id"] in our_ids}
    print(f"  Matching our bridges: {len(grid_map)} / {len(our_ids)}\n")

    bridges_updated = 0
    ev_updated      = 0
    ev_inserted     = 0

    for bridge_id, row in sorted(grid_map.items()):

        designmain   = clean(row.get("designmain",   ""))
        materialmain = clean(row.get("materialmain", ""))
        mainspans    = clean(row.get("mainspans",    ""))
        appspans     = clean(row.get("appspans",     ""))
        length       = clean(row.get("length",       ""))
        deckwidth    = clean(row.get("deckwidth",    ""))
        roadwidth    = clean(row.get("roadwidth",    ""))
        lftcurbsw    = clean(row.get("lftcurbsw",   ""))
        rtcurbsw     = clean(row.get("rtcurbsw",    ""))
        aroadwidth   = clean(row.get("aroadwidth",  ""))
        skew         = clean(row.get("skew",         "") if "skew" in row else "")
        lat          = clean(row.get("lat",          ""))
        _lon_raw     = clean(row.get("long",         ""))
        # BrM exports western longitudes as positive — flip sign
        if _lon_raw:
            try:
                _lon_f = float(_lon_raw)
                lon = str(-abs(_lon_f))
            except ValueError:
                lon = _lon_raw
        else:
            lon = None
        yearrecon    = clean(row.get("yearrecon",    ""))

        # ── Update bridges table ──────────────────────────────────────
        struct_type = " | ".join(filter(None, [designmain, materialmain])) or None

        if struct_type or lat or lon:
            if not args.dry_run:
                conn.execute("""
                    UPDATE bridges SET
                        struct_type  = COALESCE(NULLIF(?,''), struct_type),
                        lat          = COALESCE(NULLIF(?,''), lat),
                        lon          = COALESCE(NULLIF(?,''), lon),
                        updated_at   = datetime('now')
                    WHERE bridge_id = ?
                """, (struct_type, lat, lon, bridge_id))
            bridges_updated += 1

        # ── Evidence rows ─────────────────────────────────────────────
        # Span items — always write (new data not in original export)
        span_val = None
        if mainspans or appspans:
            span_val = f"Main: {mainspans or '?'}  Approach: {appspans or '0'}"

        tasks = [
            # (item_id, item_name, value, source_col, fill_gaps_only)
            ("B.SP.06", "Span Type",               designmain,   "BrM Grid: designmain (NBI 43A)",  False),
            ("B.SP.04", "Span Material",            materialmain, "BrM Grid: materialmain (NBI 43B)", False),
            ("B.SP.02", "Number of Spans",          span_val,     "BrM Grid: mainspans/appspans (NBI 45/46)", False),
            ("B.G.01",  "NBIS Bridge Length",       length,       "BrM Grid: length (NBI 49)",       True),
            ("B.G.02",  "Total Bridge Length",      length,       "BrM Grid: length (NBI 49)",       False),
            ("B.G.05",  "Bridge Width Out-to-Out",  deckwidth,    "BrM Grid: deckwidth (NBI 52)",    True),
            ("B.G.06",  "Bridge Width Curb-to-Curb",roadwidth,    "BrM Grid: roadwidth (NBI 51)",    False),
            ("B.G.07",  "Left Curb or Sidewalk Width", lftcurbsw, "BrM Grid: lftcurbsw (NBI 50A)",  True),
            ("B.G.08",  "Right Curb or Sidewalk Width", rtcurbsw, "BrM Grid: rtcurbsw (NBI 50B)",   True),
            ("B.G.09",  "Approach Roadway Width",   aroadwidth,   "BrM Grid: aroadwidth (NBI 32)",   False),
            ("B.G.11",  "Skew",                     skew,         "BrM Grid: skew (NBI 34)",         True),
        ]

        for item_id, item_name, value, source_col, fill_gaps_only in tasks:
            existing_before = conn.execute(
                "SELECT id, brm_value FROM evidence WHERE bridge_id=? AND item_id=? AND feature_id='PRIMARY'",
                (bridge_id, item_id)
            ).fetchone()

            wrote = upsert_ev(conn, bridge_id, item_id, item_name, value, source_col,
                              fill_gaps_only=fill_gaps_only, dry_run=args.dry_run)
            if wrote:
                if existing_before:
                    ev_updated += 1
                else:
                    ev_inserted += 1

    if not args.dry_run:
        conn.commit()

    conn.close()

    print(f"  Bridges updated (struct_type): {bridges_updated}")
    print(f"  Evidence rows updated:         {ev_updated}")
    print(f"  Evidence rows inserted (new):  {ev_inserted}")
    print()
    if args.dry_run:
        print("  Dry run complete — run without --dry-run to apply changes.")
    else:
        print("  Import complete.")
        print("  Next: refresh the web app to see updated structure types and BrM values.")


if __name__ == "__main__":
    main()
