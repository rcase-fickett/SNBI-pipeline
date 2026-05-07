#!/usr/bin/env python3
"""
09_discover_features.py — GIS-based feature enrichment via ODOT TransGIS and OSM.

For each bridge in the DB:
  1. Gets bridge coordinates from layer 101 (stores in bridges.lat/lon for reuse)
  2. Enriches B.F.03 plan_value for H* (carried), R*, W* features with GIS names
  3. Writes B.RR.01 railroad service type from layer 143 CARGO/STATUS
  4. Adds P01 (sidewalk) and P02 (bicycle) features if detected in layers 132/136

Conservative: never removes or renames existing features, only enriches.
All GIS-sourced names are written as plan_value APPROX so plan extraction can override.
Only fills null plan_values; safe to re-run.

Usage:
    python 09_discover_features.py
    python 09_discover_features.py --dry-run
    python 09_discover_features.py --limit 10
    python 09_discover_features.py --bridge 02283
    python 09_discover_features.py --bridge 02283 --verbose
"""

import os, sys, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH
from lib.db import get_conn, migrate_db
from lib.geo_context import get_bridge_coords, discover_features

try:
    from lib.snbi_items import ITEM_BY_ID
except ImportError:
    from snbi_items import ITEM_BY_ID

GIS_SOURCE = "ODOT TransGIS (09_discover_features)"
OSM_SOURCE = "OpenStreetMap Overpass"
DELAY_SEC  = 0.3   # pause between bridges — GIS server courtesy


# ── DB helpers ─────────────────────────────────────────────────────────────

def get_feature_ids(conn, bridge_id, prefix):
    """Return sorted distinct feature_ids starting with prefix for a bridge."""
    rows = conn.execute(
        "SELECT DISTINCT feature_id FROM evidence WHERE bridge_id=? AND feature_id LIKE ?",
        (bridge_id, f"{prefix}%")
    ).fetchall()
    return sorted(r[0] for r in rows)


def is_carried(conn, bridge_id, feature_id):
    """Return True if B.F.02 = 'C' (carried on) for this feature."""
    row = conn.execute(
        "SELECT brm_value, plan_value FROM evidence "
        "WHERE bridge_id=? AND item_id='B.F.02' AND feature_id=?",
        (bridge_id, feature_id)
    ).fetchone()
    if not row:
        return False
    val = (row["plan_value"] or row["brm_value"] or "").strip().upper()
    return val == "C"


def enrich_f03(conn, bridge_id, feature_id, gis_name, source, dry_run):
    """Write B.F.03 plan_value with GIS name if plan_value is currently null."""
    if not gis_name:
        return False
    row = conn.execute(
        "SELECT id, plan_value FROM evidence "
        "WHERE bridge_id=? AND item_id='B.F.03' AND feature_id=?",
        (bridge_id, feature_id)
    ).fetchone()
    if not row or row["plan_value"]:
        return False
    if not dry_run:
        conn.execute(
            "UPDATE evidence SET plan_value=?, plan_confidence='APPROX', "
            "brm_source_col=?, updated_at=datetime('now') WHERE id=?",
            (gis_name, source, row["id"])
        )
    return True


def fill_rr01(conn, bridge_id, feature_id, service_type, dry_run):
    """Write B.RR.01 brm_value if null. Returns 'inserted', 'updated', or None."""
    row = conn.execute(
        "SELECT id, brm_value FROM evidence "
        "WHERE bridge_id=? AND item_id='B.RR.01' AND feature_id=?",
        (bridge_id, feature_id)
    ).fetchone()
    if row:
        if row["brm_value"]:
            return None
        if not dry_run:
            conn.execute(
                "UPDATE evidence SET brm_value=?, brm_source_col=?, "
                "updated_at=datetime('now') WHERE id=?",
                (service_type, GIS_SOURCE, row["id"])
            )
        return "updated"
    else:
        if not dry_run:
            item = ITEM_BY_ID.get("B.RR.01", {})
            conn.execute(
                "INSERT OR IGNORE INTO evidence "
                "(bridge_id, item_id, feature_id, item_name, brm_value, "
                "brm_source_col, plan_confidence, status) "
                "VALUES (?,?,?,?,?,?,'APPROX','PENDING')",
                (bridge_id, "B.RR.01", feature_id,
                 item.get("name", "Railroad Service Type"),
                 service_type, GIS_SOURCE)
            )
        return "inserted"


def add_pathway_feature(conn, bridge_id, feature_id, name, source, dry_run):
    """Seed B.F.01/02/03 for a new P* feature if it doesn't already exist."""
    exists = conn.execute(
        "SELECT 1 FROM evidence WHERE bridge_id=? AND feature_id=? LIMIT 1",
        (bridge_id, feature_id)
    ).fetchone()
    if exists:
        return False
    if not dry_run:
        for item_id, val, src in [
            ("B.F.01", feature_id, source),
            ("B.F.02", "C",        source),   # pathways are carried on the bridge
            ("B.F.03", name,       source),
        ]:
            item = ITEM_BY_ID.get(item_id, {})
            conn.execute(
                "INSERT OR IGNORE INTO evidence "
                "(bridge_id, item_id, feature_id, item_name, brm_value, "
                "brm_source_col, plan_confidence, status) "
                "VALUES (?,?,?,?,?,?,'APPROX','PENDING')",
                (bridge_id, item_id, feature_id,
                 item.get("name", item_id), val, src)
            )
    return True


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true",
                        help="Preview without writing to DB")
    parser.add_argument("--limit",    type=int, default=None,
                        help="Process first N bridges only")
    parser.add_argument("--bridge",   default=None,
                        help="Process a single bridge ID")
    parser.add_argument("--verbose",  action="store_true",
                        help="Print each update")
    args = parser.parse_args()

    print("=" * 60)
    print("  Phase 9: GIS Feature Discovery")
    print("=" * 60)

    conn = get_conn(DB_PATH)
    migrate_db(DB_PATH)

    if args.bridge:
        rows = conn.execute(
            "SELECT bridge_id, lat, lon, feature_category FROM bridges WHERE bridge_id=?",
            (args.bridge,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT bridge_id, lat, lon, feature_category FROM bridges ORDER BY bridge_id"
        ).fetchall()

    if args.limit:
        rows = rows[:args.limit]

    total = len(rows)
    print(f"  Bridges to process : {total}")
    if args.dry_run:
        print("  DRY RUN - no changes written\n")

    counts = dict(
        coords_fetched=0, coords_failed=0, no_coords=0,
        f03_updated=0, rr01_written=0,
        p01_added=0, p02_added=0,
    )

    for i, bridge_row in enumerate(rows):
        bridge_id = bridge_row["bridge_id"]
        lat       = bridge_row["lat"]
        lon       = bridge_row["lon"]
        feat_cat  = bridge_row["feature_category"] or ""

        # ── Get/store coordinates ────────────────────────────────────────
        if not lat or not lon:
            lat, lon = get_bridge_coords(bridge_id)
            if lat and lon:
                counts["coords_fetched"] += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE bridges SET lat=?, lon=?, updated_at=datetime('now') "
                        "WHERE bridge_id=?",
                        (str(lat), str(lon), bridge_id)
                    )
                if args.verbose:
                    print(f"  [{bridge_id}] Coords fetched: {lat}, {lon}")
            else:
                counts["coords_failed"] += 1
                if args.verbose:
                    print(f"  [{bridge_id}] No coordinates in layer 101 - skipping")

        if not lat or not lon:
            counts["no_coords"] += 1
            continue

        lat, lon = float(lat), float(lon)

        # ── GIS discovery ────────────────────────────────────────────────
        gis = discover_features(lat, lon, feature_category=feat_cat)

        # ── H* carried features: update B.F.03 with GIS route name ──────
        if gis["h_name"]:
            for fid in get_feature_ids(conn, bridge_id, "H"):
                if not is_carried(conn, bridge_id, fid):
                    continue
                if enrich_f03(conn, bridge_id, fid, gis["h_name"], GIS_SOURCE, args.dry_run):
                    counts["f03_updated"] += 1
                    if args.verbose:
                        print(f"  [{bridge_id}] {fid} B.F.03 -> '{gis['h_name']}'")

        # ── R* features: B.F.03 rail name + B.RR.01 service type ────────
        r_fids = get_feature_ids(conn, bridge_id, "R")
        for fid, rail in zip(r_fids, gis["rail_lines"]):
            rr_name = f"{rail['name']}|{rail['abbr']}" if rail["abbr"] else rail["name"]
            if enrich_f03(conn, bridge_id, fid, rr_name, GIS_SOURCE, args.dry_run):
                counts["f03_updated"] += 1
            result = fill_rr01(conn, bridge_id, fid, rail["service_type"], args.dry_run)
            if result:
                counts["rr01_written"] += 1
                if args.verbose:
                    print(f"  [{bridge_id}] {fid} B.RR.01={rail['service_type']}  '{rr_name}'")

        # ── W* features: B.F.03 from OSM ────────────────────────────────
        if gis["waterway_name"]:
            for fid in get_feature_ids(conn, bridge_id, "W"):
                if enrich_f03(conn, bridge_id, fid, gis["waterway_name"], OSM_SOURCE, args.dry_run):
                    counts["f03_updated"] += 1
                    if args.verbose:
                        print(f"  [{bridge_id}] {fid} B.F.03 -> '{gis['waterway_name']}'")

        # ── P01: Sidewalk feature ────────────────────────────────────────
        if gis["has_sidewalk"]:
            if add_pathway_feature(conn, bridge_id, "P01", gis["sidewalk_desc"],
                                   GIS_SOURCE, args.dry_run):
                counts["p01_added"] += 1
                if args.verbose:
                    print(f"  [{bridge_id}] P01 added: '{gis['sidewalk_desc']}'")

        # ── P02: Bicycle feature ─────────────────────────────────────────
        if gis["has_bicycle"]:
            if add_pathway_feature(conn, bridge_id, "P02", gis["bicycle_desc"],
                                   GIS_SOURCE, args.dry_run):
                counts["p02_added"] += 1
                if args.verbose:
                    print(f"  [{bridge_id}] P02 added: '{gis['bicycle_desc']}'")

        # Commit every 50 bridges and print progress
        if not args.dry_run and (i + 1) % 50 == 0:
            conn.commit()
            print(f"  ... {i + 1}/{total} processed")

        time.sleep(DELAY_SEC)

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  Bridges processed        : {total - counts['no_coords']}")
    print(f"  Coordinates fetched      : {counts['coords_fetched']}")
    print(f"  Coordinates failed       : {counts['coords_failed']}")
    print(f"  B.F.03 names enriched    : {counts['f03_updated']}")
    print(f"  B.RR.01 service types    : {counts['rr01_written']}")
    print(f"  P01 sidewalk features    : {counts['p01_added']}")
    print(f"  P02 bicycle features     : {counts['p02_added']}")

    if args.dry_run:
        print("\n  Dry run complete - run without --dry-run to apply.")
    else:
        print("\n  GIS enrichment complete.")
        print("  B.F.03 values are APPROX plan_values. Plan extraction can override with HIGH.")


if __name__ == "__main__":
    main()
