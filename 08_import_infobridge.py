#!/usr/bin/env python3
"""
08_import_infobridge.py — Import NBI data pre-fills from InfoBridge export.

ID mapping: BrM BridgeNumber column == InfoBridge "8 - Structure Number" (strip quotes).
Reads the most recent Selected_Bridges_*.txt in the project directory.

SNBI item mappings (DataCrosswalk Clean/Partial transitions):
  NBI 28A  -> B.H.08 on H01         (Lanes on carried highway)
  NBI 28B  -> B.H.08 on H* below    (Lanes on below highway)
  NBI 10   -> B.H.12 on H* below    (Max usable vertical clearance, below feature)
  NBI 53   -> B.H.12 on H01         (Only when < 99.0 -- overhead structure constraint)
  NBI 54B  -> B.H.13 on H* below    (Min vertical underclearance -- highway below, per 42B)
  NBI 54B  -> B.RR.02 on R* below   (Min vertical clearance -- railroad below, per 42B)
  NBI 55B  -> B.H.15 on H* below
  NBI 56   -> B.H.14 on H* below
  NBI 55B+56 -> B.RR.03 on R* below (min of left and right)
  NBI 38   -> B.N.01 on W* below    (Navigation control -- navigable waterway Y/N)
  NBI 39   -> B.N.02 on W* below    (Navigation vertical clearance)
  NBI 40   -> B.N.04 on W* below    (Navigation horizontal clearance)
  NBI 116  -> B.N.03 on W* below    (Lift bridge clearance when raised -- plan_value/APPROX)
  struct_type -> B.N.03 = 999.9     (Bascule/swing/tilt/pivot/retractable -- plan_value/APPROX)

NOTE: B.N.03, B.N.05, and B.N.06 have no BrM column (brm_col=None).  Values for these
items are derived/inferred, so they are written to plan_value with APPROX confidence rather
than brm_value.  B.N.05 (9999.9 / 0) and B.N.06 (codes 0-5) require field inspection and
are left PENDING -- add pre-fill logic here using upsert_approx() if a reliable source
is identified.

Below features are resolved per bridge via get_below_features() using B.F.02='B', so
divided-highway bridges (where H02 is a second carried feature) are handled correctly.
NBI 42B (type of service under) routes underclearances to the matching feature type.

Confidence: APPROX for all values.
Only fills null brm_values — safe to re-run.

Usage:
    python 08_import_infobridge.py
    python 08_import_infobridge.py --dry-run
    python 08_import_infobridge.py --file path/to/export.txt
    python 08_import_infobridge.py --limit 20
"""

import os, sys, csv, glob, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH, BRM_EXPORT_PATH
from lib.db import get_conn, migrate_db, get_below_features

SOURCE = "InfoBridge NBI Export"

# NBI Item 38 Navigation Control Code → SNBI B.N.01
# FHWA codes: 1-3=navigable federal/state/other, 4=not navigable, 5=tidal, 7=not over water
NAV_CODE_MAP = {
    "1": "Y", "2": "Y", "3": "Y",
    "4": "N",
    "5": "Y",  # tidal — navigable
    "6": "Y",
    "7": "N",
}

# NBI Item 111 (Pier/Abutment Protection) is intentionally NOT mapped to B.N.06.
# SNBI B.N.06 uses codes 0-5 which require engineering assessment; the NBI legacy
# A/N values cannot be reliably translated. B.N.06 is left for inspector entry.


def clean(val):
    return str(val).strip().strip("'").strip()


def safe_float(val):
    try:
        return float(clean(val))
    except (ValueError, TypeError):
        return None


def build_id_map(brm_path):
    """Return {nbi_structure_number: bridge_id} from Bridge_List_Export BridgeNumber column."""
    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas not installed.")
        sys.exit(1)
    df = pd.read_excel(brm_path, dtype=str).fillna("")
    if "BridgeNumber" not in df.columns:
        print("ERROR: 'BridgeNumber' column not found in Bridge_List_Export.xlsx")
        sys.exit(1)
    mapping = {}
    for _, row in df.iterrows():
        nbi = clean(row["BridgeNumber"])
        bid = str(row["BridgeID"]).strip()
        if nbi and bid:
            mapping[nbi] = bid
    return mapping


def find_latest_file(script_dir):
    files = sorted(glob.glob(os.path.join(script_dir, "Selected_Bridges_*.txt")), reverse=True)
    return files[0] if files else None


def upsert_approx(conn, bridge_id, feature_id, item_id, item_name, value, reasoning,
                  dry_run, verbose):
    """
    Write value to plan_value / plan_confidence='APPROX'.
    Used for items with brm_col=None whose values are derived rather than pulled
    directly from a BrM field (e.g. B.N.03 inferred from bridge type).
    Only fills rows where plan_value is currently null/pending.
    """
    feat_exists = conn.execute(
        "SELECT 1 FROM evidence WHERE bridge_id=? AND feature_id=? LIMIT 1",
        (bridge_id, feature_id),
    ).fetchone()
    if not feat_exists:
        return None

    row = conn.execute(
        "SELECT id, plan_value, plan_confidence FROM evidence "
        "WHERE bridge_id=? AND item_id=? AND feature_id=?",
        (bridge_id, item_id, feature_id),
    ).fetchone()

    if row:
        pv = (row["plan_value"] or "").strip()
        pc = (row["plan_confidence"] or "").strip()
        if pv and pc not in ("PENDING", "NOT_FOUND", ""):
            return None  # already has a real plan value
        if not dry_run:
            conn.execute(
                "UPDATE evidence SET plan_value=?, plan_confidence='APPROX', "
                "plan_reasoning=?, updated_at=datetime('now') WHERE id=?",
                (str(value), reasoning, row["id"]),
            )
        if verbose:
            print(f"  {'DRY ' if dry_run else ''}APPROX [{bridge_id}] {feature_id} {item_id} = {value}")
        return "updated"
    else:
        if not dry_run:
            conn.execute(
                "INSERT INTO evidence "
                "(bridge_id, item_id, feature_id, item_name, plan_value, "
                " plan_confidence, plan_reasoning, brm_source_col, status) "
                "VALUES (?,?,?,?,?,'APPROX',?,?,'PENDING')",
                (bridge_id, item_id, feature_id, item_name, str(value), reasoning, SOURCE),
            )
        if verbose:
            print(f"  {'DRY ' if dry_run else ''}APPROX INSERT [{bridge_id}] {feature_id} {item_id} = {value}")
        return "inserted"


def upsert_ev(conn, bridge_id, feature_id, item_id, item_name, value, dry_run, verbose,
              auto_q=None):
    """
    Write value only if the feature exists for this bridge and brm_value is currently null.
    Returns 'inserted', 'updated', or None.
    """
    feat_exists = conn.execute(
        "SELECT 1 FROM evidence WHERE bridge_id=? AND feature_id=? LIMIT 1",
        (bridge_id, feature_id),
    ).fetchone()
    if not feat_exists:
        return None

    row = conn.execute(
        "SELECT id, brm_value FROM evidence WHERE bridge_id=? AND item_id=? AND feature_id=?",
        (bridge_id, item_id, feature_id),
    ).fetchone()

    if row:
        if row["brm_value"]:
            return None  # already populated
        if not dry_run:
            conn.execute(
                "UPDATE evidence SET brm_value=?, brm_source_col=?, auto_questions=?, "
                "updated_at=datetime('now') WHERE id=?",
                (str(value), SOURCE, auto_q, row["id"]),
            )
        if verbose:
            print(f"  {'DRY ' if dry_run else ''}UPDATE [{bridge_id}] {feature_id} {item_id} = {value}")
        return "updated"
    else:
        if not dry_run:
            conn.execute(
                "INSERT INTO evidence "
                "(bridge_id, item_id, feature_id, item_name, brm_value, "
                " brm_source_col, auto_questions, plan_confidence, status) "
                "VALUES (?,?,?,?,?,?,?,'PENDING','PENDING')",
                (bridge_id, item_id, feature_id, item_name, str(value), SOURCE, auto_q),
            )
        if verbose:
            print(f"  {'DRY ' if dry_run else ''}INSERT [{bridge_id}] {feature_id} {item_id} = {value}")
        return "inserted"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",    default=None, help="InfoBridge .txt path (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit",   type=int, default=None, help="Process first N rows")
    parser.add_argument("--verbose", action="store_true", help="Print each insert/update")
    args = parser.parse_args()

    print("=" * 60)
    print("  Phase 8: Import InfoBridge NBI Data")
    print("=" * 60)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    ib_path = args.file or find_latest_file(script_dir)
    if not ib_path or not os.path.exists(ib_path):
        print("ERROR: No Selected_Bridges_*.txt found. Use --file to specify path.")
        sys.exit(1)
    print(f"  File  : {os.path.basename(ib_path)}")

    print("  Building NBI -> BrM ID map...")
    id_map = build_id_map(BRM_EXPORT_PATH)
    print(f"  BrM bridges in map: {len(id_map)}")

    conn = get_conn(DB_PATH)
    migrate_db(DB_PATH)
    our_ids = set(r[0] for r in conn.execute("SELECT bridge_id FROM bridges").fetchall())
    print(f"  Bridges in DB     : {len(our_ids)}")

    if args.dry_run:
        print("  DRY RUN - no changes written\n")

    counts = dict(inserted=0, updated=0, skipped=0, no_map=0, not_in_db=0, rows=0)

    def track(result):
        if result == "inserted":
            counts["inserted"] += 1
        elif result == "updated":
            counts["updated"] += 1
        else:
            counts["skipped"] += 1

    def upsert(bid, fid, iid, iname, val, auto_q=None):
        track(upsert_ev(conn, bid, fid, iid, iname, val, args.dry_run, args.verbose,
                        auto_q=auto_q))

    with open(ib_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if args.limit and counts["rows"] >= args.limit:
                break
            counts["rows"] += 1

            nbi_num = clean(row.get("8 - Structure Number", ""))
            bridge_id = id_map.get(nbi_num)
            if not bridge_id:
                counts["no_map"] += 1
                continue
            if bridge_id not in our_ids:
                counts["not_in_db"] += 1
                continue

            # NBI 42B: type of service under the bridge — routes underclearances to H* or R*
            svc_under = clean(row.get("42B - Type Of Service Under Bridge Code", ""))
            # 5/6 = railroad below; 7/8 = waterway (no highway underclearance); else highway
            under_is_rr  = svc_under in ("5", "6")
            under_is_water = svc_under in ("7", "8", "9")

            # Resolve actual below feature IDs from B.F.02 in evidence table
            h_below = get_below_features(conn, bridge_id, "H")
            r_below = get_below_features(conn, bridge_id, "R")
            w_below = get_below_features(conn, bridge_id, "W")

            # Auto-question text for ambiguous NBI clearance with multiple below features
            def multi_aq(fids, label):
                if len(fids) > 1:
                    return (f"NBI gives a single {label} value but multiple below features exist "
                            f"({', '.join(fids)}). Verify which feature this applies to.")
                return None

            # ── B.H.08  Lanes on Highway ──────────────────────────
            lanes_on = clean(row.get("28A - Lanes On the Structure", ""))
            if lanes_on and lanes_on not in ("0", ""):
                upsert(bridge_id, "H01", "B.H.08", "Lanes on Highway", lanes_on)

            def ev(fid, iid, iname, val, aq=None):
                track(upsert_ev(conn, bridge_id, fid, iid, iname, val,
                                args.dry_run, args.verbose, auto_q=aq))

            lanes_under = clean(row.get("28B - Lanes Under the Structure", ""))
            if lanes_under and lanes_under not in ("0", "") and h_below:
                aq = multi_aq(h_below, "lane count")
                for fid in h_below:
                    ev(fid, "B.H.08", "Lanes on Highway", lanes_under, aq)

            # ── B.H.12  Highway Max Usable Vertical Clearance ─────
            # H01: NBI 53 — only populated for overhead-structure bridges (through trusses etc.)
            vc_over = safe_float(row.get("53 - Minimum Vertical Clearance Over Bridge Roadway (ft.)", ""))
            if vc_over is not None and vc_over < 99.0:
                upsert(bridge_id, "H01", "B.H.12", "Highway Max Usable Vertical Clearance", f"{vc_over:.1f}")

            # Below H* features: NBI 10 (inventory route min vertical clearance under bridge)
            vc_under10 = safe_float(row.get("10 - Inventory Route - Minimum Vertical Clearance (ft.)", ""))
            if vc_under10 is not None and 0 < vc_under10 < 99.0 and h_below:
                aq = multi_aq(h_below, "vertical clearance")
                for fid in h_below:
                    ev(fid, "B.H.12", "Highway Max Usable Vertical Clearance", f"{vc_under10:.1f}", aq)

            # ── B.H.13 / B.RR.02  Min Vertical Underclearance ────
            vc_54b = safe_float(row.get("54B - Minimum Vertical Underclearance (ft.)", ""))
            if vc_54b is not None and vc_54b > 0:
                if under_is_rr and r_below:
                    aq = multi_aq(r_below, "vertical clearance")
                    for fid in r_below:
                        ev(fid, "B.RR.02", "Railroad Min Vertical Clearance", f"{vc_54b:.1f}", aq)
                elif not under_is_rr and not under_is_water and h_below:
                    aq = multi_aq(h_below, "vertical clearance")
                    for fid in h_below:
                        ev(fid, "B.H.13", "Highway Minimum Vertical Clearance", f"{vc_54b:.1f}", aq)

            # ── B.H.14 / B.H.15  Min Lateral Underclearances ─────
            hc_left  = safe_float(row.get("56 - Minimum Lateral Underclearance on Left (ft.)", ""))
            hc_right = safe_float(row.get("55B - Minimum Lateral Underclearance on Right (ft.)", ""))
            if not under_is_rr and not under_is_water and h_below:
                aq = multi_aq(h_below, "lateral clearance")
                if hc_left is not None and hc_left > 0:
                    for fid in h_below:
                        ev(fid, "B.H.14", "Highway Min Horizontal Clearance Left", f"{hc_left:.1f}", aq)
                if hc_right is not None and hc_right > 0:
                    for fid in h_below:
                        ev(fid, "B.H.15", "Highway Min Horizontal Clearance Right", f"{hc_right:.1f}", aq)

            # ── B.RR.03  Railroad Min Horizontal Offset ───────────
            if under_is_rr and r_below and (hc_left or hc_right):
                nonzero = [v for v in (hc_left, hc_right) if v and v > 0]
                if nonzero:
                    aq = multi_aq(r_below, "horizontal offset")
                    for fid in r_below:
                        ev(fid, "B.RR.03", "Railroad Min Horizontal Offset", f"{min(nonzero):.1f}", aq)

            # ── Waterway / Navigation items ───────────────────────
            nav_code = clean(row.get("38 - Navigation Control Code", ""))
            nav_val  = NAV_CODE_MAP.get(nav_code)
            if nav_val and w_below:
                for fid in w_below:
                    ev(fid, "B.N.01", "Navigable Waterway", nav_val)

            nav_vc = safe_float(row.get("39 - Navigation Vertical Clearance (ft.)", ""))
            if nav_vc is not None and nav_vc > 0 and w_below:
                for fid in w_below:
                    ev(fid, "B.N.02", "Navigation Min Vertical Clearance", f"{nav_vc:.1f}")

            lift_vc = safe_float(row.get("116 - Minimum Vertical Clearance - Lift Bridge (ft.)", ""))
            if lift_vc is not None and 0 < lift_vc < 99.0 and w_below:
                lift_reasoning = (
                    f"NBI Item 116 (Minimum Vertical Clearance — Lift Bridge): {lift_vc:.1f} ft. "
                    "B.N.03 has no BrM column; value stored as APPROX plan value."
                )
                for fid in w_below:
                    track(upsert_approx(conn, bridge_id, fid, "B.N.03",
                                        "Movable Bridge Max Nav Vertical Clearance",
                                        f"{lift_vc:.1f}", lift_reasoning,
                                        args.dry_run, args.verbose))

            nav_hc = safe_float(row.get("40 - Navigation Horizontal Clearance (ft.)", ""))
            if nav_hc is not None and nav_hc > 0 and w_below:
                for fid in w_below:
                    ev(fid, "B.N.04", "Navigation Channel Width", f"{nav_hc:.1f}")

            # B.N.06 (Substructure Navigation Protection) is NOT imported from InfoBridge.
            # NBI Item 111 uses legacy A/N codes that do not map to SNBI codes 0-5.
            # SNBI codes 0-1 also require a formal engineering assessment, not field obs.
            # Leave as PENDING for inspector determination.

    # ── B.N.03: migrate any legacy brm_value rows → plan_value/APPROX ────────
    # Earlier runs wrote 999.9 to brm_value; B.N.03 has no BrM column so those
    # should live in plan_value instead.
    migrated = 0
    if not args.dry_run:
        migrated = conn.execute("""
            UPDATE evidence
            SET plan_value      = COALESCE(NULLIF(plan_value,''), brm_value),
                plan_confidence = CASE
                    WHEN plan_confidence IN ('PENDING','NOT_FOUND','') OR plan_confidence IS NULL
                    THEN 'APPROX'
                    ELSE plan_confidence
                END,
                brm_value       = NULL,
                updated_at      = datetime('now')
            WHERE item_id   = 'B.N.03'
              AND brm_value IS NOT NULL
        """).rowcount
    if migrated:
        print(f"  Migrated {migrated} existing B.N.03 brm_value row(s) -> plan_value/APPROX")

    # ── B.N.03: 999.9 for bascule/swing/tilt/pivot/retractable bridges ───────
    # These bridge types provide unlimited clearance in the open position.
    # Vertical lift bridges (Movable-Lift) are excluded — they have a specific
    # raised clearance already sourced from NBI 116 above.
    # Written to plan_value/APPROX (not brm_value) because B.N.03 has no BrM column.
    UNLIMITED_CLEARANCE_KEYWORDS = [
        "bascule", "swing", "tilt", "thrust", "pivot", "retractable",
    ]
    unlimited_bridges = conn.execute("""
        SELECT DISTINCT b.bridge_id, b.struct_type
        FROM bridges b
        JOIN evidence e ON e.bridge_id = b.bridge_id
        WHERE e.item_id = 'B.N.03'
          AND (e.plan_value IS NULL
               OR e.plan_confidence IN ('PENDING','NOT_FOUND','')
               OR e.plan_confidence IS NULL)
    """).fetchall()

    unlimited_count = 0
    for brow in unlimited_bridges:
        st = (brow["struct_type"] or "").lower()
        if any(kw in st for kw in UNLIMITED_CLEARANCE_KEYWORDS):
            reasoning = (
                f"Bridge type '{brow['struct_type']}' provides unlimited vertical clearance "
                "in the open position — code 999.9 per SNBI. "
                "B.N.03 has no BrM column; value stored as APPROX plan value."
            )
            w_feats = [r["feature_id"] for r in conn.execute(
                "SELECT feature_id FROM evidence WHERE bridge_id=? AND item_id='B.N.03'",
                (brow["bridge_id"],),
            ).fetchall()]
            for fid in w_feats:
                result = upsert_approx(conn, brow["bridge_id"], fid, "B.N.03",
                                       "Movable Bridge Max Nav Vertical Clearance",
                                       "999.9", reasoning, args.dry_run, args.verbose)
                if result:
                    label = "[DRY RUN] " if args.dry_run else ""
                    print(f"  {label}{brow['bridge_id']} ({brow['struct_type']}): B.N.03 = 999.9 (APPROX)")
                    unlimited_count += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  InfoBridge rows read       : {counts['rows']}")
    print(f"  B.N.03 unlimited clearance : {unlimited_count}")
    print(f"  No NBI->BrM mapping       : {counts['no_map']}")
    print(f"  Not in our DB             : {counts['not_in_db']}")
    print(f"  Evidence rows inserted    : {counts['inserted']}")
    print(f"  Evidence rows updated     : {counts['updated']}")
    print(f"  Evidence rows skipped     : {counts['skipped']}")

    if args.dry_run:
        print("\n  Dry run - run without --dry-run to apply.")
    else:
        print("\n  Import complete. All values written as APPROX.")
        print("  Phase 2 plan extraction will override with HIGH-confidence values.")


if __name__ == "__main__":
    main()
