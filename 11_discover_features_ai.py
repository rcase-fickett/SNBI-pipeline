#!/usr/bin/env python3
"""
11_discover_features_ai.py — AI-assisted feature inventory seeding via GIS data.

For each bridge, queries ODOT TransGIS and OSM (no PDFs), then asks Claude to
enumerate all features (B.F.01/02/03). Seeds missing feature rows as APPROX
plan_values so Phase 2 plan extraction can override with HIGH confidence.

Feature types detected:
  H##  Highway (including ramps above/below)
  R##  Railroad
  P##  Pathway (sidewalks, trails, bike facilities)
  W##  Waterway
  D##  Dry terrain / side slope
  X##  Other

Only inserts rows for feature_ids that do not already exist in the evidence table.
Safe to re-run (idempotent). Phase 2 can override any APPROX value with HIGH.

Run order: after Phase 9 (09_discover_features.py) and Phase 10, before Phase 2.

Usage:
    python 11_discover_features_ai.py
    python 11_discover_features_ai.py --dry-run
    python 11_discover_features_ai.py --bridge 02682
    python 11_discover_features_ai.py --limit 10 --verbose
    python 11_discover_features_ai.py --status BRM_DONE
"""

import os, sys, time, argparse, json, re
import anthropic

# Force UTF-8 output so Claude responses with Unicode don't crash on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DB_PATH, ANTHROPIC_API_KEY, CLAUDE_MODEL, BATCH_DELAY_SEC
from lib.db import get_conn
from lib.geo_context import gather_feature_context, find_nearby_bridges
from lib.snbi_items import ITEM_BY_ID

SOURCE = "ODOT TransGIS + OSM (11_discover_features_ai)"

MEDIAN_DESCS = {
    1: "none / undivided",
    2: "mountable / flush median (closed)",
    3: "painted median",
    4: "raised curbed median (open)",
    5: "raised median with openings",
    6: "raised solid median",
    7: "physical barrier",
    8: "curbed / closed median",
    9: "vegetation median",
    10: "depressed median",
    11: "Jersey barrier / concrete barrier",
}

SYSTEM_PROMPT = (
    "You are an expert at coding SNBI (Special Notice Bridge Inspection) bridge features "
    "from GIS data. Return only valid JSON arrays. No explanation, no markdown, no preamble."
)

# Items seeded as PENDING (null plan_value) for each feature type — filled by Phase 2 / inspector
_TYPE_ITEMS = {
    "H": ("B.H.08", "B.H.12", "B.H.13", "B.H.14", "B.H.15", "B.H.16"),
    "R": ("B.RR.01", "B.RR.02", "B.RR.03"),
    "W": ("B.N.01", "B.N.02", "B.N.03", "B.N.04", "B.N.05", "B.N.06"),
}

# Known values for carried-on H* clearance items (not applicable — pre-filled as APPROX)
_CARRIED_H_PREFILLS = {
    "B.H.12": "99.9",
    "B.H.13": "99.9",
    "B.H.14": "Not reported (carried-on feature)",
    "B.H.15": "Not reported (carried-on feature)",
}


def get_existing_features(conn, bridge_id):
    """
    Return sorted list of (feature_id, location, name) tuples already in evidence.
    Excludes PRIMARY and WORK rows. Location from B.F.02, name from B.F.03.
    """
    rows = conn.execute(
        """SELECT DISTINCT e.feature_id,
              (SELECT COALESCE(plan_value, brm_value) FROM evidence
               WHERE bridge_id=e.bridge_id AND item_id='B.F.02'
                 AND feature_id=e.feature_id LIMIT 1) AS loc,
              (SELECT COALESCE(plan_value, brm_value) FROM evidence
               WHERE bridge_id=e.bridge_id AND item_id='B.F.03'
                 AND feature_id=e.feature_id LIMIT 1) AS name
           FROM evidence e
           WHERE e.bridge_id=?
             AND e.feature_id NOT IN ('PRIMARY')
             AND e.feature_id NOT LIKE 'WORK:%'
           ORDER BY e.feature_id""",
        (bridge_id,)
    ).fetchall()
    return [(r[0], r[1] or "", r[2] or "") for r in rows]


def build_prompt(ctx, existing_features):
    """
    existing_features: list of (feature_id, location, name) tuples already in the DB.
    """
    existing_fids = [fid for fid, _, __ in existing_features]

    lines = [
        f"Identify all SNBI features for bridge {ctx['bridge_id']} using the GIS data below.",
        "",
        "SNBI Feature Coding Rules:",
        "- B.F.01 Feature Type: H##=Highway, R##=Railroad, P##=Pathway (sidewalk/trail/bike),",
        "  W##=Waterway, D##=Dry terrain, X##=Other.",
        "  Number sequentially within each type starting at 01: H01, H02... W01, W02... P01...",
        "- B.F.02 Location: C=Carried on bridge, B=Below bridge, A=Above bridge.",
        "- B.F.03 Name: pipe-delimited, route number first.",
        "  Append NB/SB/EB/WB when a highway is divided into separate directional carriageways.",
        "  Example: 'I-84 EB|US-30 EB|Columbia River Highway'",
        "- A divided highway (physical or mountable median) = two H features, one per direction.",
        "- One P feature per location covers both left and right sidewalks; name them 'Sidewalks'.",
        "",
        "DATA SOURCE RULES — strictly follow this hierarchy:",
        "1. ODOT CARRIES/CROSSES are the ONLY authoritative source for H (highway),",
        "   R (railroad), and W (waterway) features. Do NOT add H/R/W features based on",
        "   nearby road names, signed routes, or OSM ways — those queries cast a wide net",
        "   and will pick up features from adjacent structures that do NOT cross this bridge.",
        "2. Pathway (P*) features may be added from ODOT sidewalk/bike layers and OSM",
        "   footway/path/cycleway tags — these are not in CARRIES/CROSSES.",
        "3. If CARRIES/CROSSES mentions ramps but lacks enough detail to name them accurately",
        "   (origin and destination), return [] for those ramp features. Phase 2 plan drawings",
        "   will enumerate ramps with HIGH confidence.",
        "4. OSM and road data are provided for name clarification only, not feature discovery.",
    ]

    LOC_LABEL = {"C": "carried on", "B": "below", "A": "above"}

    if existing_features:
        lines.append(
            "\nFeature IDs already seeded in the database "
            "(do NOT return these — they are complete, do NOT rename them):"
        )
        for fid, loc, name in existing_features:
            loc_str = LOC_LABEL.get(loc, loc or "?")
            lines.append(f"  {fid} ({loc_str}): {name or '(name pending — Phase 2 will fill)'}")
        lines.append(
            "Return ONLY NEW feature IDs not listed above. Continue the sequence within each type "
            "(e.g. if H01 and H02 exist, new highway features start at H03)."
        )
    else:
        lines.append("\nNo features have been seeded yet — return all features.")

    lines += ["", "GIS Data:"]

    if ctx["carries"]:
        lines.append(f"  ODOT CARRIES (authoritative — features ON this bridge): {ctx['carries']}")
    if ctx["crosses"]:
        lines.append(f"  ODOT CROSSES (authoritative — features BELOW this bridge): {ctx['crosses']}")

    if ctx["median_code"] is not None:
        desc = MEDIAN_DESCS.get(ctx["median_code"], f"code {ctx['median_code']}")
        lines.append(
            f"  Median detected: {desc} (MEDN_CD={ctx['median_code']}) "
            f"— use only if CARRIES confirms a divided highway"
        )

    if ctx["sidewalk_sides"]:
        lines.append(f"  Sidewalks detected: {', '.join(ctx['sidewalk_sides'])} side(s) (ODOT layer 132)")
    if ctx["bike_types"]:
        lines.append(f"  Bike facilities: {', '.join(ctx['bike_types'])} (ODOT layer 136)")
    if ctx["rail_names"]:
        lines.append(f"  Railroads nearby: {', '.join(ctx['rail_names'])} (ODOT layer 143 — confirm against CROSSES)")

    lines.append("  Supporting context (for name clarification only — do not add H/R/W features from these):")
    if ctx["route_names"]:
        lines.append(f"    Signed routes: {', '.join(ctx['route_names'])}")
    if ctx["roads"]:
        lines.append(f"    Nearby roads: {', '.join(ctx['roads'])}")

    if ctx["osm_features"]:
        lines.append(
            "    OSM ways (bridge=yes = on a bridge structure; "
            "layer: 1+=above, 0=grade, -1=below):"
        )
        for f in ctx["osm_features"][:20]:
            label = " | ".join(filter(None, [f["ref"], f["name"]])) or "(unnamed)"
            ftype = f["highway"] or f["railway"] or f["waterway"]
            parts = [f"name={label!r}"]
            if ftype:
                parts.append(f"type={ftype}")
            if f["bridge"]:
                parts.append(f"bridge={f['bridge']}")
            if f["layer"] not in ("0", ""):
                parts.append(f"layer={f['layer']}")
            if f["oneway"] in ("yes", "-1"):
                parts.append("oneway=yes")
            lines.append(f"      - {', '.join(parts)}")

    lines += [
        "",
        'Return a JSON array. Each object must have exactly: {"feature_id": "H02", "location": "C", "name": "..."}',
        "Only include features you are confident about. Omit uncertain features.",
        "If no new features are needed, return [].",
    ]

    return "\n".join(lines)


def _extract_json_array(text):
    """
    Extract the first JSON array from text that may contain reasoning prose.
    Returns parsed list or None.
    """
    # Try the whole text first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown fences
    if "```" in text:
        for block in text.split("```")[1::2]:
            cleaned = block.lstrip("json").strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
    # Find first [...] span that parses as JSON
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def call_claude(client, model, prompt, delay, debug=False):
    """Send a text-only prompt to Claude and return parsed JSON list, or [] on failure."""
    if debug:
        print("\n--- PROMPT ---")
        print(prompt)
        print("--- END PROMPT ---\n")
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if debug:
            print(f"--- RAW RESPONSE ---\n{raw}\n--- END RESPONSE ---\n")
        result = _extract_json_array(raw)
        time.sleep(delay)
        return result if isinstance(result, list) else []
    except anthropic.RateLimitError:
        time.sleep(15)
        return []
    except Exception as e:
        if debug:
            print(f"API error: {e}")
        time.sleep(delay)
        return []


def _insert_row(conn, bridge_id, item_id, feature_id, val, confidence, dry_run):
    """
    INSERT OR IGNORE one evidence row. val=None inserts a PENDING placeholder row.
    Returns 1 if a row was inserted (or would be in dry-run), 0 if already existed.
    """
    item = ITEM_BY_ID.get(item_id, {})
    if not dry_run:
        cur = conn.execute(
            "INSERT OR IGNORE INTO evidence "
            "(bridge_id, item_id, feature_id, item_name, plan_value, "
            "plan_confidence, brm_source_col, status) "
            "VALUES (?,?,?,?,?,?,?,'PENDING')",
            (bridge_id, item_id, feature_id,
             item.get("name", item_id), val, confidence, SOURCE),
        )
        return cur.rowcount
    return 1  # dry-run: count as if inserted


def seed_features(conn, bridge_id, features, dry_run):
    """
    Insert evidence rows for each feature returned by Claude.
    Uses INSERT OR IGNORE — never overwrites existing rows.

    For every feature, seeds B.F.01/02/03 plus all type-specific items:
      H*: B.H.08/12/13/14/15/16/18 (carried-on clearance items pre-filled as APPROX;
          others seeded as PENDING for Phase 2 / inspector)
      R*: B.RR.01/02/03 (PENDING)
      W*: B.N.01/02/03/04/05/06 (PENDING)

    Returns count of rows inserted.
    """
    inserted = 0
    for feat in features:
        fid  = (feat.get("feature_id") or "").strip()
        loc  = (feat.get("location")   or "").strip().upper()
        name = (feat.get("name")       or "").strip()

        if not fid or loc not in ("C", "B", "A", "T", "L"):
            continue

        ftype = fid[0] if fid else ""

        # B.F.01/02/03 — always required, skip if value missing
        for item_id, val in (("B.F.01", fid), ("B.F.02", loc), ("B.F.03", name)):
            if val:
                inserted += _insert_row(conn, bridge_id, item_id, fid, val, "APPROX", dry_run)

        # Type-specific items
        for item_id in _TYPE_ITEMS.get(ftype, ()):
            if ftype == "H" and loc == "C" and item_id in _CARRIED_H_PREFILLS:
                # Known not-applicable value for carried-on highway clearance items
                val        = _CARRIED_H_PREFILLS[item_id]
                confidence = "APPROX"
            else:
                # Unknown — create PENDING placeholder for Phase 2 / inspector
                val        = None
                confidence = "PENDING"
            inserted += _insert_row(conn, bridge_id, item_id, fid, val, confidence, dry_run)

    return inserted


def seed_h18_crossings(conn, bridge_id, lat, lon, dry_run):
    """
    Seed B.H.18 (Crossing Bridge Number) for each bridge detected within 100m.
    feature_id = crossing bridge ID; plan_value = crossing bridge ID.
    Uses INSERT OR IGNORE — safe to re-run.
    Returns count of rows inserted.
    """
    nearby = find_nearby_bridges(bridge_id, lat, lon, radius_m=200)
    inserted = 0
    for nb in nearby:
        crossing_id = nb["bridge_id"]
        if not crossing_id:
            continue
        inserted += _insert_row(conn, bridge_id, "B.H.18", crossing_id, crossing_id, "APPROX", dry_run)
    return inserted


def run_backfill(args):
    """
    Backfill type-specific PENDING rows for H*/R*/W* features that were seeded
    before Phase 11 (e.g. by Phase 9 or Phase 1 BrM import) and are missing items
    like B.H.08, B.H.16, B.RR.02, B.RR.03, B.N.02-06.
    Also cleans up B.H.18 rows incorrectly seeded per H*/R*/W* feature_id and
    re-seeds B.H.18 via proximity detection (feature_id = crossing bridge ID).
    Uses INSERT OR IGNORE — never overwrites existing rows.
    """
    conn = get_conn(DB_PATH)

    print("=" * 60)
    print("  Phase 11: Type-Specific Item Backfill + B.H.18 Re-seed")
    print("=" * 60)
    if args.dry_run:
        print("  DRY RUN - no changes written\n")

    # ── Step 1: Clean up B.H.18 rows incorrectly keyed to H*/R*/W*/P* feature_ids ──
    # Correct feature_ids for B.H.18 are numeric bridge IDs (e.g. "02682").
    # Wrong ones start with a letter (H01, H02, R01, W01, P01, etc.).
    wrong_count = conn.execute(
        "SELECT COUNT(*) FROM evidence "
        "WHERE item_id='B.H.18' AND feature_id GLOB '[A-Z]*'"
    ).fetchone()[0]

    if wrong_count:
        print(f"  Removing {wrong_count} B.H.18 row(s) incorrectly keyed to feature_ids...")
        if not args.dry_run:
            conn.execute(
                "DELETE FROM evidence WHERE item_id='B.H.18' AND feature_id GLOB '[A-Z]*'"
            )
            conn.commit()
    else:
        print("  No incorrectly-keyed B.H.18 rows found.")

    # ── Step 2: Type-specific item backfill ──────────────────────────────────────
    candidates = conn.execute("""
        SELECT DISTINCT e.bridge_id, e.feature_id,
               COALESCE(
                   MAX(CASE WHEN e.item_id='B.F.02' THEN e.plan_value END),
                   MAX(CASE WHEN e.item_id='B.F.02' THEN e.brm_value END)
               ) AS loc
        FROM evidence e
        WHERE e.feature_id NOT IN ('PRIMARY')
          AND e.feature_id NOT LIKE 'WORK:%'
          AND SUBSTR(e.feature_id, 1, 1) IN ('H', 'R', 'W')
        GROUP BY e.bridge_id, e.feature_id
        ORDER BY e.bridge_id, e.feature_id
    """).fetchall()

    if args.bridge:
        candidates = [r for r in candidates if r["bridge_id"] == args.bridge]

    print(f"  Features to check  : {len(candidates)}")

    features_updated = type_rows = 0

    for row in candidates:
        bridge_id = row["bridge_id"]
        fid       = row["feature_id"]
        loc       = (row["loc"] or "").strip().upper()
        ftype     = fid[0]

        existing_items = set(r[0] for r in conn.execute(
            "SELECT item_id FROM evidence WHERE bridge_id=? AND feature_id=?",
            (bridge_id, fid)
        ).fetchall())

        inserted = 0
        for item_id in _TYPE_ITEMS.get(ftype, ()):
            if item_id in existing_items:
                continue
            if ftype == "H" and loc == "C" and item_id in _CARRIED_H_PREFILLS:
                val, confidence = _CARRIED_H_PREFILLS[item_id], "APPROX"
            else:
                val, confidence = None, "PENDING"
            inserted += _insert_row(conn, bridge_id, item_id, fid, val, confidence, args.dry_run)

        if inserted:
            features_updated += 1
            type_rows        += inserted
            label = "[DRY RUN] " if args.dry_run else ""
            if args.verbose or args.bridge:
                print(f"  {label}[{bridge_id}] {fid}: +{inserted} row(s)")

    if not args.dry_run:
        conn.commit()

    # ── Step 3: B.H.18 proximity seeding ─────────────────────────────────────────
    bridge_rows = conn.execute(
        "SELECT bridge_id, lat, lon FROM bridges WHERE lat IS NOT NULL AND lon IS NOT NULL"
        + (" AND bridge_id=?" if args.bridge else "")
        + " ORDER BY bridge_id",
        (args.bridge,) if args.bridge else ()
    ).fetchall()

    print(f"  Bridges for B.H.18 : {len(bridge_rows)} (have coordinates)")

    h18_bridges = h18_rows = 0
    import time as _time

    for br in bridge_rows:
        bid = br["bridge_id"]
        try:
            lat, lon = float(br["lat"]), float(br["lon"])
        except (ValueError, TypeError):
            continue

        n = seed_h18_crossings(conn, bid, lat, lon, args.dry_run)
        if n:
            h18_bridges += 1
            h18_rows    += n
            label = "[DRY RUN] " if args.dry_run else ""
            if args.verbose or args.bridge:
                print(f"  {label}[{bid}] B.H.18: +{n} crossing bridge(s)")
        _time.sleep(0.2)  # courtesy delay for ODOT GIS server

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  B.H.18 rows removed  : {wrong_count}")
    print(f"  Features updated     : {features_updated}")
    print(f"  Type-item rows added : {type_rows}")
    print(f"  Bridges w/ B.H.18    : {h18_bridges}")
    print(f"  B.H.18 rows added    : {h18_rows}")
    if args.dry_run:
        print("\n  Dry run complete - run without --dry-run to apply.")
    else:
        print("\n  Backfill complete.")


def run(args):
    api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY not set. Check config.py or environment.")

    client = anthropic.Anthropic(api_key=api_key)
    conn   = get_conn(DB_PATH)

    if args.bridge:
        rows = conn.execute(
            "SELECT bridge_id, lat, lon FROM bridges WHERE bridge_id=?",
            (args.bridge,)
        ).fetchall()
    elif args.status:
        rows = conn.execute(
            "SELECT bridge_id, lat, lon FROM bridges "
            "WHERE processing_status=? ORDER BY RANDOM()",
            (args.status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT bridge_id, lat, lon FROM bridges ORDER BY bridge_id"
        ).fetchall()

    if args.limit:
        rows = rows[:args.limit]

    total = len(rows)
    print("=" * 60)
    print("  Phase 11: AI Feature Discovery")
    print("=" * 60)
    print(f"  Bridges to process : {total}")
    if args.dry_run:
        print("  DRY RUN - no changes written\n")

    no_coords = skipped = bridges_updated = total_rows = h18_total = 0

    for bridge_row in rows:
        bridge_id = bridge_row["bridge_id"]
        lat       = bridge_row["lat"]
        lon       = bridge_row["lon"]

        if not lat or not lon:
            no_coords += 1
            if args.verbose:
                print(f"  [{bridge_id}] No coordinates - skipped")
            continue

        try:
            lat, lon = float(lat), float(lon)
        except (ValueError, TypeError):
            no_coords += 1
            continue

        existing = get_existing_features(conn, bridge_id)

        ctx    = gather_feature_context(bridge_id, lat, lon)
        prompt = build_prompt(ctx, existing)

        if args.verbose:
            existing_fids = [fid for fid, _, __ in existing]
            print(f"  [{bridge_id}] Existing features: {existing_fids or '(none)'}")

        features = call_claude(client, CLAUDE_MODEL, prompt, BATCH_DELAY_SEC, debug=args.debug)

        if not features:
            skipped += 1
            if args.verbose:
                print(f"  [{bridge_id}] Claude returned no new features")
        else:
            n = seed_features(conn, bridge_id, features, args.dry_run)

            if n:
                bridges_updated += 1
                total_rows      += n
                label = "[DRY RUN] " if args.dry_run else ""
                print(f"  {label}[{bridge_id}] +{len(features)} feature(s), {n} row(s) inserted:")
                for f in features:
                    print(f"    {f.get('feature_id')} ({f.get('location')}) {f.get('name')}")
            else:
                if args.verbose:
                    print(f"  [{bridge_id}] All returned features already exist")

        # B.H.18 — proximity-based crossing bridge detection (no API call)
        n_h18 = seed_h18_crossings(conn, bridge_id, lat, lon, args.dry_run)
        h18_total += n_h18
        if n_h18 and args.verbose:
            print(f"  [{bridge_id}] B.H.18: +{n_h18} crossing bridge(s)")

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  Bridges processed    : {total - no_coords}")
    print(f"  No coordinates       : {no_coords}")
    print(f"  Nothing new to add   : {skipped}")
    print(f"  Bridges updated      : {bridges_updated}")
    print(f"  Evidence rows added  : {total_rows}")
    print(f"  B.H.18 rows added    : {h18_total}")
    if args.dry_run:
        print("\n  Dry run complete - run without --dry-run to apply.")
    else:
        print("\n  Phase 11 complete.")
        print("  New features are APPROX. Phase 2 can override with HIGH from plan drawings.")


def main():
    parser = argparse.ArgumentParser(
        description="Phase 11: AI feature discovery from GIS data"
    )
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--bridge",   metavar="ID",
                        help="Process a single bridge ID")
    parser.add_argument("--limit",    type=int, default=None,
                        help="Process first N bridges only")
    parser.add_argument("--status",   metavar="STATUS",
                        help="Filter by bridge status (e.g. BRM_DONE, PLANS_DONE)")
    parser.add_argument("--verbose",  action="store_true",
                        help="Print details for all bridges including no-change ones")
    parser.add_argument("--debug",    action="store_true",
                        help="Print full prompt and raw Claude response for each bridge")
    parser.add_argument("--backfill", action="store_true",
                        help="Backfill missing type-specific items (B.H.08/16, B.RR.02/03, "
                             "B.N.02-06) for existing H*/R*/W* features; fix B.H.18 keying; "
                             "re-seed B.H.18 via proximity detection")
    args = parser.parse_args()
    if args.backfill:
        run_backfill(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
