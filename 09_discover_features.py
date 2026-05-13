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
from lib.geo_context import get_bridge_coords, discover_features, query_lane_counts

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


def get_bf03_value(conn, bridge_id, feature_id):
    """Return the best available B.F.03 value (plan_value preferred, then brm_value)."""
    row = conn.execute(
        "SELECT plan_value, brm_value FROM evidence "
        "WHERE bridge_id=? AND item_id='B.F.03' AND feature_id=?",
        (bridge_id, feature_id)
    ).fetchone()
    if not row:
        return ""
    return (row["plan_value"] or row["brm_value"] or "").strip()


def fill_bh08(conn, bridge_id, feature_id, lanes, source, dry_run, force=False):
    """
    Write B.H.08 brm_value. Returns True if written.

    Normally only fills null values. Pass force=True for GIS-split highways: when a
    single BrM/InfoBridge feature has been divided into multiple directional carriageways
    by GIS, the BrM total-lane count is wrong for each individual feature and must be
    replaced with the per-carriageway GIS value.
    """
    row = conn.execute(
        "SELECT id, brm_value FROM evidence "
        "WHERE bridge_id=? AND item_id='B.H.08' AND feature_id=?",
        (bridge_id, feature_id)
    ).fetchone()
    if not row:
        return False
    if not force and row["brm_value"]:
        return False
    if not dry_run:
        conn.execute(
            "UPDATE evidence SET brm_value=?, brm_source_col=?, "
            "updated_at=datetime('now') WHERE id=?",
            (str(lanes), source, row["id"])
        )
    return True


def pick_lane_entry(gis_lanes, feature_name, exclude_roadway_ids=None):
    """
    Return (entry_dict, matched_hwynumb, fallback_used) for the best-matching GIS segment.

    Skips entries whose roadway_id is in exclude_roadway_ids so that successive H features
    on the same divided highway each receive a distinct directional carriageway segment.
    If exclusion exhausts all candidates the full list is used and fallback_used=True is
    returned, signalling that the two features may share the same source segment.

    Matching priority (applied to the filtered candidate list):
      1. HWYNUMB from layer 126 appears anywhere in feature_name
      2. Single unique count → use it regardless of name
      3. Multiple counts, no name match → most frequent; tie-break smallest
    """
    if not gis_lanes:
        return None, "", False

    exclude = exclude_roadway_ids or set()
    candidates = [
        e for e in gis_lanes
        if e.get("roadway_id") is None or e.get("roadway_id") not in exclude
    ]
    fallback = False
    if not candidates:
        candidates = gis_lanes
        fallback = True

    fname_upper = feature_name.upper()

    # Try HWYNUMB match
    for entry in candidates:
        hwy = entry["hwynumb"]
        if hwy and hwy in fname_upper:
            return entry, hwy, fallback

    # Single unique count
    from collections import Counter
    unique = list({e["no_lanes"] for e in candidates})
    if len(unique) == 1:
        return candidates[0], "", fallback

    # Multiple counts — most frequent; tie-break smallest
    freq = Counter(e["no_lanes"] for e in candidates)
    max_freq = max(freq.values())
    best = min(k for k, v in freq.items() if v == max_freq)
    for entry in candidates:
        if entry["no_lanes"] == best:
            return entry, "", fallback

    return candidates[0], "", fallback


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
        bh08_written=0,
        lane_flags=0,
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

        # ── H* B.H.08: Lane counts from ODOT TransGIS layers 126/347 ────
        h_fids = get_feature_ids(conn, bridge_id, "H")
        if h_fids:
            gis_lanes = query_lane_counts(lat, lon)
            if gis_lanes:
                # ── Detect GIS-split highways ────────────────────────────────
                # A highway is "split" if BrM's single feature has been divided
                # into multiple directional features by GIS (e.g. I-205 NB + SB).
                # In that case the BrM total-lane count is wrong per-feature and
                # GIS must override it.
                #
                # Two signals, unioned:
                #   1. GIS returns 2+ distinct RDWY_IDs for the same HWYNUMB
                #   2. 2+ H features match the same non-empty HWYNUMB
                from collections import defaultdict, Counter as _Counter
                rdwy_per_hwy: dict = defaultdict(set)
                for _e in gis_lanes:
                    if _e["hwynumb"] and _e.get("roadway_id") is not None:
                        rdwy_per_hwy[_e["hwynumb"]].add(_e["roadway_id"])
                split_by_gis = {
                    hwy for hwy, rids in rdwy_per_hwy.items() if len(rids) > 1
                }

                # Pre-compute hwy_key for each H feature (no exclusion yet)
                hwy_keys: dict = {}
                for fid in h_fids:
                    fname = get_bf03_value(conn, bridge_id, fid)
                    _, hk, _ = pick_lane_entry(gis_lanes, fname)
                    hwy_keys[fid] = (fname, hk)
                hk_counts = _Counter(hk for _, hk in hwy_keys.values() if hk)
                split_by_features = {hk for hk, c in hk_counts.items() if c > 1}

                split_highways = split_by_gis | split_by_features

                # ── Assign per-feature lane counts ───────────────────────────
                used_per_hwy: dict = {}
                for fid in h_fids:
                    fname, hwy_key = hwy_keys[fid]
                    exclude = used_per_hwy.setdefault(hwy_key, set())

                    entry, _, fallback = pick_lane_entry(gis_lanes, fname,
                                                         exclude_roadway_ids=exclude)
                    if not entry:
                        continue

                    if fallback:
                        print(f"  [{bridge_id}] {fid} B.H.08 WARN: fallback to "
                              f"already-used segment (only one {hwy_key!r} segment "
                              f"within radius) — values may be duplicated")

                    rid = entry.get("roadway_id")
                    if rid is not None:
                        exclude.add(rid)

                    chosen = entry["no_lanes"]
                    force  = hwy_key in split_highways
                    wrote  = fill_bh08(conn, bridge_id, fid, chosen, GIS_SOURCE,
                                       args.dry_run, force=force)
                    if wrote:
                        counts["bh08_written"] += 1
                    if args.verbose and wrote:
                        tag = (" [SPLIT]" if force else "") + (" [FALLBACK]" if fallback else "")
                        print(f"  [{bridge_id}] {fid} B.H.08={chosen}  "
                              f"(GIS roadway_id={rid}, matched '{fname}'){tag}")

                    # Write SNBI Errata note to every processed B.H.08 row,
                    # including ones whose brm_value was already set (e.g. H01).
                    # Always overwrites — this is a system-generated message that should
                    # reflect the current GIS state on every Phase 9 run.
                    if not args.dry_run:
                        row_id = conn.execute(
                            "SELECT id FROM evidence "
                            "WHERE bridge_id=? AND item_id='B.H.08' AND feature_id=?",
                            (bridge_id, fid)
                        ).fetchone()
                        if row_id:
                            conn.execute(
                                "UPDATE evidence SET auto_questions=?, "
                                "updated_at=datetime('now') WHERE id=?",
                                (
                                    f"GIS lane count ({chosen}). Per SNBI Errata, verify: "
                                    f"count only lanes striped or operating as full-width "
                                    f"traffic lanes (including auxiliary lanes and special use "
                                    f"lanes) that run the entire length of the bridge. Exclude "
                                    f"turn lanes or tapers that do not span the full bridge.",
                                    row_id["id"],
                                ),
                            )

        # ── Lane-count validation: flag feature-count mismatches ─────────
        # Compare GIS lane count (per road segment) against BrM NBI 28A
        # (total lanes on structure) to catch over-counted or missing features.
        #
        # Divided highway: ODOT LRS records one directional segment at the
        # bridge, so GIS < BrM total → a second carried-on H feature may
        # be needed.
        # Duplicate features: if GIS × 1 already equals BrM but multiple
        # carried-on H features exist, the extras are likely duplicates.
        if h_fids and gis_lanes:
            gis_max = max(e["no_lanes"] for e in gis_lanes)

            # BrM total from H01 B.H.08 (InfoBridge NBI 28A)
            brm_row = conn.execute("""
                SELECT brm_value FROM evidence
                WHERE bridge_id=? AND item_id='B.H.08' AND feature_id='H01'
                  AND brm_source_col LIKE '%InfoBridge%'
            """, (bridge_id,)).fetchone()

            if brm_row and brm_row["brm_value"]:
                try:
                    brm_total = int(brm_row["brm_value"])
                except ValueError:
                    brm_total = None

                if brm_total and brm_total > 0:
                    carried_fids = [
                        fid for fid in h_fids if is_carried(conn, bridge_id, fid)
                    ]
                    n_carried = len(carried_fids)
                    flag_msg = None

                    if gis_max < brm_total and n_carried == 1:
                        # GIS shows fewer lanes than BrM total → likely divided
                        flag_msg = (
                            f"GIS lane count ({gis_max}) is less than BrM total lanes on "
                            f"structure ({brm_total}). This may indicate a divided highway "
                            f"where only one directional carriageway is captured in GIS — "
                            f"verify whether a second carried-on H feature (e.g. EB/WB) is needed."
                        )
                    elif n_carried > 1 and gis_max >= brm_total:
                        # GIS count alone accounts for all BrM lanes → extras likely duplicate
                        flag_msg = (
                            f"GIS lane count ({gis_max}) accounts for all BrM lanes on "
                            f"structure ({brm_total}), but {n_carried} carried-on H features "
                            f"exist. Verify whether multiple H features are warranted — "
                            f"possible duplicate features created by Phase 11."
                        )

                    if flag_msg:
                        counts["lane_flags"] += 1
                        if args.verbose:
                            print(f"  [{bridge_id}] LANE FLAG: {flag_msg[:80]}...")
                        if not args.dry_run:
                            conn.execute("""
                                UPDATE evidence
                                SET auto_questions = ?,
                                    updated_at     = datetime('now')
                                WHERE bridge_id=? AND item_id='B.H.08' AND feature_id='H01'
                                  AND (auto_questions IS NULL OR auto_questions = '')
                            """, (flag_msg, bridge_id))

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
    print(f"  B.H.08 lane counts       : {counts['bh08_written']}")
    print(f"  Lane-count flags         : {counts['lane_flags']}")

    if args.dry_run:
        print("\n  Dry run complete - run without --dry-run to apply.")
    else:
        print("\n  GIS enrichment complete.")
        print("  B.F.03 values are APPROX plan_values. Plan extraction can override with HIGH.")


if __name__ == "__main__":
    main()
