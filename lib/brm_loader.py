"""
brm_loader.py — Load BrM export data into the evidence table
Extracts everything available from the Excel exports without any API calls.
"""
import json
import pandas as pd
from pathlib import Path

# Import from lib (when run as part of package)
try:
    from lib.snbi_items import ITEM_BY_ID, PRIMARY_ITEMS, FEATURE_ITEMS, classify_feature
    from lib.db import upsert_bridge, upsert_evidence, set_bridge_status, log
except ImportError:
    from snbi_items import ITEM_BY_ID, PRIMARY_ITEMS, FEATURE_ITEMS, classify_feature
    from db import upsert_bridge, upsert_evidence, set_bridge_status, log


def load_brm_data(conn, config):
    """
    Main entry point. Reads both Excel files and populates bridges +
    evidence tables with all available BrM values.
    """
    print("Loading Bridge_List_Export.xlsx ...")
    brm = pd.read_excel(config.BRM_EXPORT_PATH, dtype=str).fillna("")

    print("Loading Bridge_List.xlsx ...")
    bl  = pd.read_excel(config.BRIDGE_LIST_PATH,  dtype=str).fillna("")

    print("Loading Vertical_Clearance_List.xlsx ...")
    vc  = pd.read_excel(config.VC_LIST_PATH, dtype=str).fillna("")

    # Build lookup sets
    target_ids = set(
        bl[bl[config.COMPLETE_COL] == config.COMPLETE_VALUE][config.BRIDGE_ID_COL].tolist()
    )
    vc_found = set(vc[vc["Found"] == "Found"]["Bridge List"].tolist())
    brm_lookup = {row["BridgeID"]: row for _, row in brm.iterrows()}

    if config.BRIDGE_FILTER:
        target_ids = target_ids & set(config.BRIDGE_FILTER)

    print(f"  Target bridges: {len(target_ids)}")
    print(f"  Bridges with VC docs: {len(vc_found & target_ids)}")

    bridges_root = Path(config.BRIDGES_ROOT)
    processed = 0

    for bridge_id in sorted(target_ids):
        brm_row = brm_lookup.get(bridge_id, {})

        # ── Resolve file paths ────────────────────────────────────
        bridge_dir  = bridges_root / bridge_id
        plan_pdf    = bridge_dir / f"{bridge_id} Plans.pdf"
        plan_path   = str(plan_pdf) if plan_pdf.exists() else None

        # Vertical clearance PDFs (pattern: {id}_BC_*.pdf)
        vc_paths = []
        if bridge_dir.exists():
            vc_paths = [str(p) for p in bridge_dir.glob(f"{bridge_id}_BC_*.pdf")]

        feature_cat = classify_feature(brm_row.get("FeatureIntersected", ""))

        # ── Upsert bridge record ──────────────────────────────────
        upsert_bridge(conn, {
            "bridge_id":          bridge_id,
            "bridge_name":        brm_row.get("Name", ""),
            "facility_carried":   brm_row.get("FacilityCarried", ""),
            "feature_intersected":brm_row.get("FeatureIntersected", ""),
            "feature_category":   feature_cat,
            "county":             brm_row.get("CountyCode", ""),
            "year_built":         brm_row.get("YearBuilt", ""),
            "struct_type":        brm_row.get("LifecyclePhase", ""),
            "has_vc_doc":         1 if bridge_id in vc_found else 0,
            "plan_pdf_path":      plan_path,
            "vc_pdf_paths":       json.dumps(vc_paths) if vc_paths else "[]",
            "processing_status":  "PENDING",
        })

        # ── Load PRIMARY items from BrM ───────────────────────────
        for item in PRIMARY_ITEMS:
            col = item.get("brm_col")
            brm_val = brm_row.get(col, "") if col else ""
            upsert_evidence(conn, {
                "bridge_id":     bridge_id,
                "item_id":       item["id"],
                "feature_id":    "PRIMARY",
                "item_name":     item["name"],
                "brm_value":     brm_val or None,
                "brm_source_col":col,
                "plan_confidence":"PENDING",
                "status":        "PENDING",
            })

        # ── Infer features from BrM ───────────────────────────────
        features = _infer_features(brm_row, feature_cat)

        # ── Load FEATURE items ────────────────────────────────────
        for feat in features:
            fid = feat["feature_id"]
            for item in FEATURE_ITEMS:
                applies = item.get("applies_to", "ALL")
                feat_type_char = fid[0]  # H, W, R, P, D, X
                if applies != "ALL" and applies != feat_type_char:
                    continue

                col      = item.get("brm_col")
                brm_val  = brm_row.get(col, "") if col else ""

                # Special case: B.H.12/13 = 99.9 if feature is CARRIED ON
                auto_q   = None
                if item["id"] in ("B.H.12","B.H.13") and feat.get("location") == "C":
                    brm_val  = "99.9"
                if item["id"] in ("B.H.14","B.H.15") and feat.get("location") == "C":
                    brm_val  = "Not reported (carried-on feature)"

                # B.N.02-06 conditional on B.N.01
                if item["id"] in ("B.N.02","B.N.03","B.N.04","B.N.05","B.N.06"):
                    auto_q = "Only populate if B.N.01 = Y (navigable waterway confirmed)"

                upsert_evidence(conn, {
                    "bridge_id":     bridge_id,
                    "item_id":       item["id"],
                    "feature_id":    fid,
                    "item_name":     item["name"],
                    "brm_value":     brm_val or None,
                    "brm_source_col":col,
                    "auto_questions":auto_q,
                    "plan_confidence":"PENDING",
                    "status":        "PENDING",
                })

            # Store F.01 / F.02 / F.03 seed values from BrM
            _seed_feature_identification(conn, bridge_id, feat)

        # ── Work items (B.W.02/03) — no BrM source ───────────────
        for item in [i for i in ITEM_BY_ID.values() if i["id"] in ("B.W.02","B.W.03")]:
            upsert_evidence(conn, {
                "bridge_id":    bridge_id,
                "item_id":      item["id"],
                "feature_id":   "WORK:unknown",
                "item_name":    item["name"],
                "plan_confidence":"PENDING",
                "status":       "PENDING",
                "auto_questions":"Year must come from plans/revision block — not available in BrM export",
            })

        set_bridge_status(conn, bridge_id, "BRM_DONE")
        log(conn, bridge_id, "BRM", "SUCCESS", f"Loaded {len(features)} features")
        processed += 1
        if processed % 50 == 0:
            conn.commit()
            print(f"  ... {processed}/{len(target_ids)} bridges loaded")

    conn.commit()
    print(f"\nBrM load complete: {processed} bridges, evidence rows populated.")


def _infer_features(brm_row, feature_cat):
    """
    Build a minimal feature list from BrM data.
    Returns list of dicts: {feature_id, type_char, location, name}
    """
    features = []

    carried = brm_row.get("FacilityCarried", "").strip()
    crossed = brm_row.get("FeatureIntersected", "").strip()

    # H01 — highway carried on (always present)
    features.append({
        "feature_id": "H01",
        "type_char":  "H",
        "location":   "C",
        "name":       carried or "Unknown highway",
    })

    # Feature below — type depends on what's crossed
    if crossed:
        if feature_cat == "WATERWAY":
            features.append({"feature_id":"W01","type_char":"W","location":"B","name":crossed})
        elif feature_cat == "RAILROAD":
            features.append({"feature_id":"R01","type_char":"R","location":"B","name":crossed})
        elif feature_cat == "HIGHWAY":
            features.append({"feature_id":"H02","type_char":"H","location":"B","name":crossed})

    return features


def _seed_feature_identification(conn, bridge_id, feat):
    """Write BrM-derived F.01 / F.02 / F.03 values as brm_value seeds."""
    fid = feat["feature_id"]
    seeds = [
        ("B.F.01", feat["feature_id"],    "Inferred from BrM FacilityCarried/FeatureIntersected"),
        ("B.F.02", feat["location"],       "Inferred from BrM — C=carried on, B=below"),
        ("B.F.03", feat["name"],           "BrM FacilityCarried / FeatureIntersected"),
    ]
    for item_id, val, src in seeds:
        item = ITEM_BY_ID[item_id]
        upsert_evidence(conn, {
            "bridge_id":     bridge_id,
            "item_id":       item_id,
            "feature_id":    fid,
            "item_name":     item["name"],
            "brm_value":     val,
            "brm_source_col":src,
            "plan_confidence":"PENDING",
            "status":        "PENDING",
        })
