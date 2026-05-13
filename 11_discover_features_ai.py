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
from lib.geo_context import gather_feature_context, f03_with_direction
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
    "H": ("B.H.08", "B.H.12", "B.H.13", "B.H.14", "B.H.15", "B.H.16", "B.H.18"),
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
              (SELECT COALESCE(plan_value, gis_value, brm_value) FROM evidence
               WHERE bridge_id=e.bridge_id AND item_id='B.F.02'
                 AND feature_id=e.feature_id LIMIT 1) AS loc,
              (SELECT COALESCE(plan_value, gis_value, brm_value) FROM evidence
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
        "- B.F.03 Name: Use the GIS-derived base names provided in the GIS Data section below.",
        "  These follow SNBI spec: route designation(s) first (most recognizable first),",
        "  then official/common name, pipe-separated. Example: 'I-205|East Portland Freeway'.",
        "  For a divided feature (NB/SB or EB/WB), append the direction to each route",
        "  designation but NOT to the common name. Example: 'I-205 NB|East Portland Freeway'.",
        "  For pathways use 'Sidewalks', 'Bike Lane', 'Trail', etc. as appropriate.",
        "- A divided highway (physical or mountable median) = two H features, one per direction.",
        "- One P feature per location covers both left and right sidewalks; name them 'Sidewalks'.",
        "",
        "DATA SOURCE RULES — strictly follow this hierarchy:",
        "1. ODOT CARRIES/CROSSES are the ONLY authoritative source for H (highway),",
        "   R (railroad), and W (waterway) features. Do NOT add H/R/W features based on",
        "   nearby road names, signed routes, or OSM ways — those queries cast a wide net",
        "   and will pick up features from adjacent structures that do NOT cross this bridge.",
        "   CRITICAL — W (waterway) rule: Do NOT create a W feature unless CROSSES explicitly",
        "   names a waterway (creek, river, stream, canal, etc.). Road names that reference",
        "   water geography (e.g. 'Johnson Creek Blvd', 'River Road', 'Mill Creek Drive')",
        "   are roads named after nearby water bodies — they are NOT evidence that this bridge",
        "   crosses water. If CROSSES only lists highways, railroads, or roads, return NO W features.",
        "2. Pathway (P*) features may be added from ODOT sidewalk/bike layers and OSM",
        "   footway/path/cycleway tags — these are not in CARRIES/CROSSES.",
        "3. If CARRIES/CROSSES mentions ramps but lacks enough detail to name them accurately",
        "   (origin and destination), return [] for those ramp features. Phase 2 plan drawings",
        "   will enumerate ramps with HIGH confidence.",
        "4. OSM and road data are provided for name clarification only, not feature discovery.",
        "5. Divided highway rule: A divided highway requires TWO H features, one per direction",
        "   (NB/SB or EB/WB). 'Divided' determinations are pre-computed from ODOT data (see",
        "   GIS Data section below) — trust them. If a highway is marked divided and only one",
        "   undirected feature exists for it, add the second direction as a NEW feature.",
        "   Do NOT create directional features for roads not explicitly marked as divided below.",
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
        divided_note = ""
        if ctx.get("carries_divided"):
            divided_note = f" ← DIVIDED HIGHWAY ({ctx['carries_divided_reason']})"
        f03 = ctx.get("carries_f03_base") or ctx["carries"]
        lines.append(f"  ODOT CARRIES (features ON this bridge): {ctx['carries']}{divided_note}")
        lines.append(f"    B.F.03 base name: \"{f03}\"")
        if ctx.get("carries_divided"):
            nb = f03_with_direction(f03, "NB")
            sb = f03_with_direction(f03, "SB")
            lines.append(f"    Divided → use \"{nb}\" / \"{sb}\" (or EB/WB based on orientation)")
    if ctx["crosses"]:
        divided_note = ""
        if ctx.get("crosses_divided"):
            divided_note = f" ← DIVIDED HIGHWAY ({ctx['crosses_divided_reason']})"
        f03 = ctx.get("crosses_f03_base") or ctx["crosses"]
        lines.append(f"  ODOT CROSSES (features BELOW/ABOVE this bridge): {ctx['crosses']}{divided_note}")
        lines.append(f"    B.F.03 base name: \"{f03}\"")
        if ctx.get("crosses_divided"):
            nb = f03_with_direction(f03, "NB")
            sb = f03_with_direction(f03, "SB")
            lines.append(f"    Divided → use \"{nb}\" / \"{sb}\" (or EB/WB based on orientation)")

    if ctx["sidewalk_sides"]:
        lines.append(f"  Sidewalks detected: {', '.join(ctx['sidewalk_sides'])} side(s) (ODOT layer 132)")
    if ctx["bike_types"]:
        lines.append(f"  Bike facilities: {', '.join(ctx['bike_types'])} (ODOT layer 136)")
    if ctx["rail_names"]:
        lines.append(f"  Railroads nearby: {', '.join(ctx['rail_names'])} (ODOT layer 143 — confirm against CROSSES)")

    # Lane counts — authoritative for B.H.08; group by route, take max per route
    lane_counts = ctx.get("lane_counts") or []
    if lane_counts:
        by_route = {}
        for lc in lane_counts:
            key = lc["hwynumb"] or "(local)"
            by_route[key] = max(by_route.get(key, 0), lc["no_lanes"])
        summaries = []
        for route, n in sorted(by_route.items()):
            label = f"Hwy {route}" if route != "(local)" else "Local road"
            summaries.append(f"{label}={n} lanes")
        lines.append(
            f"  ODOT travel lane counts (authoritative for B.H.08): {', '.join(summaries)}"
        )

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
    Non-null values go into gis_value/gis_source so plan_value stays clean for Phase 2.
    Returns 1 if a row was inserted (or would be in dry-run), 0 if already existed.
    """
    item = ITEM_BY_ID.get(item_id, {})
    if not dry_run:
        if val is not None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO evidence "
                "(bridge_id, item_id, feature_id, item_name, gis_value, gis_source, "
                "plan_confidence, status) "
                "VALUES (?,?,?,?,?,?,'PENDING','PENDING')",
                (bridge_id, item_id, feature_id,
                 item.get("name", item_id), val, SOURCE),
            )
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO evidence "
                "(bridge_id, item_id, feature_id, item_name, "
                "plan_confidence, status) "
                "VALUES (?,?,?,?,'PENDING','PENDING')",
                (bridge_id, item_id, feature_id, item.get("name", item_id)),
            )
        return cur.rowcount
    return 1  # dry-run: count as if inserted


def _gis_lane_count(feature_name, lane_counts):
    """
    Match a GIS lane count to a feature by route number in its name.
    For state highways: extract digits from name and match to hwynumb.
    For non-state / local roads: use the highest unnamed (hwynumb='') count.
    Returns int or None. Takes the max count for a route (mainline > ramps).
    """
    if not lane_counts:
        return None

    # Build per-route max dict
    by_route = {}
    for lc in lane_counts:
        key = lc["hwynumb"] or ""
        by_route[key] = max(by_route.get(key, 0), lc["no_lanes"])

    # Try to match by route number found in the feature name
    nums = re.findall(r'\b(\d+)\b', feature_name or "")
    for num in nums:
        stripped = num.lstrip("0")
        if stripped in by_route:
            return by_route[stripped]

    # Fall back to local road count (no hwynumb) — used for carried non-state roads
    if "" in by_route:
        return by_route[""]

    return None


def seed_features(conn, bridge_id, features, lane_counts, dry_run):
    """
    Insert evidence rows for each feature returned by Claude.
    Uses INSERT OR IGNORE — never overwrites existing rows.

    For every feature, seeds B.F.01/02/03 plus all type-specific items:
      H*: B.H.08 pre-filled as APPROX from GIS lane counts when available;
          clearance items for carried-on pre-filled as APPROX (99.9 / Not reported);
          remaining items seeded as PENDING for Phase 2 / inspector.
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
        gis_lanes = _gis_lane_count(name, lane_counts) if ftype == "H" else None

        for item_id in _TYPE_ITEMS.get(ftype, ()):
            if ftype == "H" and loc == "C" and item_id in _CARRIED_H_PREFILLS:
                # Known not-applicable value for carried-on highway clearance items
                val        = _CARRIED_H_PREFILLS[item_id]
                confidence = "APPROX"
            elif ftype == "H" and item_id == "B.H.08" and gis_lanes is not None:
                # Pre-fill lane count from ODOT GIS road inventory — authoritative
                val        = str(gis_lanes)
                confidence = "APPROX"
            else:
                # Unknown — create PENDING placeholder for Phase 2 / inspector
                val        = None
                confidence = "PENDING"
            inserted += _insert_row(conn, bridge_id, item_id, fid, val, confidence, dry_run)

    return inserted


def run_backfill(args):
    """
    Backfill type-specific PENDING rows for H*/R*/W* features that were seeded
    before Phase 11 (e.g. by Phase 9 or Phase 1 BrM import) and are missing items
    like B.H.08, B.H.16, B.H.18, B.RR.02, B.RR.03, B.N.02-06.
    B.H.18 is seeded for all H* features including carried-on.
    Also removes any B.H.18 rows incorrectly keyed to crossing bridge IDs or 'NONE'.
    Also seeds B.F.01/02/03 placeholder rows for any H*/R*/W* features that are
    missing them ("orphaned" features created by earlier pipeline phases without
    seeding the B.F base items first).
    Uses INSERT OR IGNORE — never overwrites existing rows.
    """
    conn = get_conn(DB_PATH)

    print("=" * 60)
    print("  Phase 11: Type-Specific Item Backfill + B.H.18 Re-seed")
    print("=" * 60)
    if args.dry_run:
        print("  DRY RUN - no changes written\n")

    # ── Step 1: Remove B.H.18 rows not keyed to H* feature IDs ─────────────────
    # B.H.18 belongs on non-carried H* features only (feature_id like H01, H02...).
    # Previous runs may have left rows keyed to crossing bridge IDs or 'NONE' — delete them.
    wrong_count = conn.execute(
        "SELECT COUNT(*) FROM evidence "
        "WHERE item_id='B.H.18' AND feature_id NOT GLOB 'H[0-9]*'"
    ).fetchone()[0]

    if wrong_count:
        print(f"  Removing {wrong_count} B.H.18 row(s) not keyed to H* features...")
        if not args.dry_run:
            conn.execute(
                "DELETE FROM evidence WHERE item_id='B.H.18' AND feature_id NOT GLOB 'H[0-9]*'"
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

    # ── Step 3: Seed B.F.01/02/03 for orphaned feature IDs ──────────────────────
    # Some H*/R*/W* feature IDs were created by earlier pipeline phases (Phase 7
    # bridge log, Phase 9 GIS) that wrote type-specific items (e.g. B.H.14, B.H.08)
    # without first seeding the B.F base rows. Phase 11 main run and this backfill
    # also added items to those orphans without fixing the gap. Seed them here.
    #
    # B.F.01 = feature_id itself (definitional — always correct).
    # B.F.02 = PENDING — Phase 2 plan extraction will determine C/B/A.
    # B.F.03 = PENDING — Phase 2 plan extraction will fill the name.
    orphan_query = conn.execute("""
        SELECT DISTINCT e.bridge_id, e.feature_id
        FROM evidence e
        WHERE e.feature_id NOT IN ('PRIMARY')
          AND e.feature_id NOT LIKE 'WORK:%'
          AND SUBSTR(e.feature_id, 1, 1) IN ('H', 'R', 'W')
          AND NOT EXISTS (
              SELECT 1 FROM evidence b
              WHERE b.bridge_id  = e.bridge_id
                AND b.feature_id = e.feature_id
                AND b.item_id    = 'B.F.01'
          )
        ORDER BY e.bridge_id, e.feature_id
    """).fetchall()

    if args.bridge:
        orphan_query = [r for r in orphan_query if r["bridge_id"] == args.bridge]

    orphan_count = len(orphan_query)
    print(f"\n  Orphaned features (missing B.F.01/02/03): {orphan_count}")

    bf_rows = orphans_fixed = 0
    for row in orphan_query:
        bridge_id = row["bridge_id"]
        fid       = row["feature_id"]
        label     = "[DRY RUN] " if args.dry_run else ""

        # B.F.01 — value IS the feature_id (e.g. "H02")
        bf_rows += _insert_row(conn, bridge_id, "B.F.01", fid, fid, "APPROX", args.dry_run)
        # B.F.02 / B.F.03 — unknown; leave as PENDING for Phase 2 / inspector
        bf_rows += _insert_row(conn, bridge_id, "B.F.02", fid, None, "PENDING", args.dry_run)
        bf_rows += _insert_row(conn, bridge_id, "B.F.03", fid, None, "PENDING", args.dry_run)
        orphans_fixed += 1
        if args.verbose or args.bridge:
            print(f"  {label}[{bridge_id}] {fid}: seeded B.F.01/02/03 placeholders")

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  B.H.18 rows removed  : {wrong_count}")
    print(f"  Features updated     : {features_updated}")
    print(f"  Type-item rows added : {type_rows}")
    print(f"  Orphans fixed        : {orphans_fixed}")
    print(f"  B.F rows added       : {bf_rows}")
    if args.dry_run:
        print("\n  Dry run complete - run without --dry-run to apply.")
    else:
        print("\n  Backfill complete.")


def reseed_bridge_gis(conn, bridge_id, api_key=None):
    """
    Run Phase 11 AI feature discovery for a single bridge using an open connection.
    Returns dict with counts: features_added, rows_inserted, error (or None).
    Used by the Flask /api/bridge/<id>/reseed-gis endpoint.
    """
    key = api_key or ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"features_added": 0, "rows_inserted": 0, "error": "ANTHROPIC_API_KEY not set"}

    bridge_row = conn.execute(
        "SELECT bridge_id, lat, lon FROM bridges WHERE bridge_id=?", (bridge_id,)
    ).fetchone()
    if not bridge_row:
        return {"features_added": 0, "rows_inserted": 0, "error": f"Bridge {bridge_id} not found"}

    lat, lon = bridge_row["lat"], bridge_row["lon"]
    try:
        lat, lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return {"features_added": 0, "rows_inserted": 0, "error": "No coordinates for bridge"}

    try:
        client   = anthropic.Anthropic(api_key=key)
        existing = get_existing_features(conn, bridge_id)
        ctx      = gather_feature_context(bridge_id, lat, lon)
        prompt   = build_prompt(ctx, existing)
        features = call_claude(client, CLAUDE_MODEL, prompt, BATCH_DELAY_SEC)
    except Exception as e:
        return {"features_added": 0, "rows_inserted": 0, "error": str(e)}

    if not features:
        return {"features_added": 0, "rows_inserted": 0, "error": None}

    n = seed_features(conn, bridge_id, features, ctx.get("lane_counts", []), dry_run=False)
    conn.commit()
    return {"features_added": len(features), "rows_inserted": n, "error": None}


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

    no_coords = skipped = bridges_updated = total_rows = 0

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
            n = seed_features(conn, bridge_id, features, ctx.get("lane_counts", []), args.dry_run)

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

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  Bridges processed    : {total - no_coords}")
    print(f"  No coordinates       : {no_coords}")
    print(f"  Nothing new to add   : {skipped}")
    print(f"  Bridges updated      : {bridges_updated}")
    print(f"  Evidence rows added  : {total_rows}")
    if args.dry_run:
        print("\n  Dry run complete - run without --dry-run to apply.")
    else:
        print("\n  Phase 11 complete.")
        print("  New features are APPROX. Phase 2 can override with HIGH from plan drawings.")


_DIR_RE = re.compile(r'\s+(NB|SB|EB|WB)$', re.IGNORECASE)


_DIR_COMPLEMENT = {"NB": "SB", "SB": "NB", "EB": "WB", "WB": "EB"}


def _hwy_num_from_name(name):
    """Extract stripped highway number from either 'Hwy 064' or 'I-205|SR-64|...' formats."""
    import re as _re
    m = _re.search(r'[Hh][Ww][Yy]\.?\s*0*(\d+)', name or "")
    if m:
        return m.group(1)
    m = _re.search(r'\bSR-(\d+)\b', name or "")
    return m.group(1) if m else None


def _count_h_features_for_hwy(conn, bridge_id, hwynumb):
    """Count H* B.F.03 rows on a bridge whose name matches a given stripped highway number."""
    rows = conn.execute("""
        SELECT COALESCE(e.brm_value, e.gis_value) AS name
        FROM evidence e
        WHERE e.bridge_id=? AND e.item_id='B.F.03' AND e.feature_id LIKE 'H%'
    """, (bridge_id,)).fetchall()
    return sum(1 for r in rows if _hwy_num_from_name(r["name"] or "") == hwynumb)


def run_reseed_divided(args):
    """
    Identify bridges where a divided highway exists in GIS but fewer than 2 H features are
    seeded for that highway, then call reseed_bridge_gis() (Phase 11 AI) for each.

    Supports --dry-run, --bridge, --limit.
    """
    from lib.geo_context import gather_feature_context, _hwy_num

    conn = get_conn(DB_PATH)

    if args.bridge:
        candidates = conn.execute("""
            SELECT DISTINCT b.bridge_id, b.lat, b.lon
            FROM bridges b
            JOIN evidence e ON e.bridge_id = b.bridge_id
            WHERE e.item_id='B.F.03' AND e.feature_id LIKE 'H%'
              AND b.bridge_id = ?
        """, (args.bridge,)).fetchall()
    else:
        candidates = conn.execute("""
            SELECT DISTINCT b.bridge_id, b.lat, b.lon
            FROM bridges b
            JOIN evidence e ON e.bridge_id = b.bridge_id
            WHERE e.item_id='B.F.03' AND e.feature_id LIKE 'H%'
            ORDER BY b.bridge_id
        """).fetchall()

    if args.limit:
        candidates = candidates[:args.limit]

    print("=" * 60)
    print("  Phase 11: Reseed Divided Highway Bridges")
    print("=" * 60)
    if args.dry_run:
        print("  DRY RUN — no changes written\n")

    checked = reseeded = skipped = 0

    for br in candidates:
        bridge_id = br["bridge_id"]
        try:
            lat, lon = float(br["lat"]), float(br["lon"])
        except (ValueError, TypeError):
            continue

        try:
            ctx = gather_feature_context(bridge_id, lat, lon)
        except Exception as e:
            print(f"  [{bridge_id}] GIS error: {e}")
            continue

        checked += 1
        needs_reseed = False
        reason_parts = []

        for field in ("carries", "crosses"):
            text    = ctx.get(field) or ""
            divided = ctx.get(f"{field}_divided") or False
            if not divided or not text:
                continue
            num = _hwy_num(text)
            if not num:
                continue
            count = _count_h_features_for_hwy(conn, bridge_id, num)
            if count < 2:
                needs_reseed = True
                reason_parts.append(
                    f"{field}={text!r} divided, only {count} H feature(s) for Hwy {num}"
                )

        if not needs_reseed:
            continue

        reason = "; ".join(reason_parts)
        label  = "[DRY RUN] " if args.dry_run else ""
        print(f"  {label}[{bridge_id}] reseed — {reason}")

        if not args.dry_run:
            result = reseed_bridge_gis(conn, bridge_id)
            if result.get("error"):
                print(f"    ERROR: {result['error']}")
            else:
                print(f"    → {result['features_added']} features, {result['rows_inserted']} rows")
        reseeded += 1

    conn.close()
    print(f"\n  Bridges checked (has H features + coords) : {checked}")
    print(f"  Bridges {'flagged' if args.dry_run else 'reseeded'}                     : {reseeded}")
    if args.dry_run:
        print("\n  Dry run — run without --dry-run to apply.")
    else:
        print("\n  Done.")


def run_rename_f03(args):
    """
    Update B.F.03 gis_value for existing features using GIS-derived names.
    Only touches rows where plan_value IS NULL (plan extraction hasn't confirmed a name yet).

    Two-pass per bridge:
      Pass 1 — compute new base names from GIS (no direction yet).
      Pass 2 — resolve directions via sibling inference, grouped by (hwynumb, location).
               If both siblings are undirected and highway is confirmed divided, defaults
               NB to the lower feature_id and flags for inspector review.
    """
    from lib.geo_context import (build_f03_name as _build, f03_with_direction as _directed,
                                  _hwy_num, gather_feature_context, detect_divided)

    conn = get_conn(DB_PATH)

    if args.bridge:
        bridges = conn.execute(
            "SELECT bridge_id, lat, lon FROM bridges WHERE bridge_id=?", (args.bridge,)
        ).fetchall()
    else:
        bridges = conn.execute(
            "SELECT bridge_id, lat, lon FROM bridges ORDER BY bridge_id"
        ).fetchall()
    if args.limit:
        bridges = bridges[:args.limit]

    print("=" * 60)
    print("  Phase 11: B.F.03 GIS Name Refresh")
    print("=" * 60)
    if args.dry_run:
        print("  DRY RUN - no changes written\n")

    updated = flagged = no_coords = 0

    for br in bridges:
        bridge_id = br["bridge_id"]
        try:
            lat, lon = float(br["lat"]), float(br["lon"])
        except (ValueError, TypeError):
            no_coords += 1
            continue

        # Fetch B.F.03 rows + their B.F.02 location codes in one query
        rows = conn.execute("""
            SELECT e.id, e.feature_id, e.plan_value, e.gis_value, e.brm_value,
                   COALESCE(loc.plan_value, loc.gis_value, loc.brm_value) AS location
            FROM evidence e
            LEFT JOIN evidence loc
                ON  loc.bridge_id  = e.bridge_id
                AND loc.feature_id = e.feature_id
                AND loc.item_id    = 'B.F.02'
            WHERE e.bridge_id=? AND e.item_id='B.F.03'
            ORDER BY e.feature_id
        """, (bridge_id,)).fetchall()

        # ── Pass 1: compute new base names ───────────────────────────────────
        # Each entry: {row_id, feature_id, current, direction, new_base, hwynumb, location}
        pass1 = []
        for r in rows:
            current = (r["plan_value"] or r["gis_value"] or r["brm_value"] or "").strip()
            if not current:
                continue

            # Extract direction: trailing suffix first (BrM format: "Hwy 064 SB"),
            # then embedded in first route token (GIS format: "I-205 NB|SR-64 NB|...")
            dm = _DIR_RE.search(current)
            if dm:
                direction = dm.group(1).upper()
                base_text = current[:dm.start()].strip()
            else:
                direction = None
                base_text = current
                if "|" in base_text:
                    first_route = base_text.split("|")[0].strip()
                    dm2 = _DIR_RE.search(first_route)
                    if dm2:
                        direction = dm2.group(1).upper()

            # Prefer brm_value for highway number extraction (always "Hwy XXX" from Phase 1)
            brm_text = (r["brm_value"] or "").strip()
            num = _hwy_num(brm_text) or _hwy_num_from_name(base_text)
            if not num:
                continue  # local road — no highway number to look up
            # Strip any direction from brm_text before using as fallback — avoids
            # build_f03_name() returning "HWY 91 NB" which then gets NB appended again
            brm_base = _DIR_RE.sub("", brm_text).strip() if brm_text else ""
            new_base  = _build(num, lat, lon, brm_base or base_text)
            pass1.append({
                "row_id":       r["id"],
                "feature_id":   r["feature_id"],
                "current":      current,
                "existing_gis": (r["gis_value"] or "").strip(),
                "direction":    direction,
                "new_base":     new_base,
                "hwynumb":      num,
                "location":     (r["location"] or "").strip(),
            })

        # ── Pass 2: resolve directions via sibling inference ─────────────────
        from collections import defaultdict
        groups = defaultdict(list)
        for entry in pass1:
            key = (entry["hwynumb"], entry["location"])
            groups[key].append(entry)

        for (hwynumb, loc), grp in groups.items():
            if len(grp) != 2:
                continue  # ramps / single feature — skip

            a, b = grp[0], grp[1]

            if a["direction"] and not b["direction"]:
                b["direction"] = _DIR_COMPLEMENT.get(a["direction"])
            elif b["direction"] and not a["direction"]:
                a["direction"] = _DIR_COMPLEMENT.get(b["direction"])
            elif not a["direction"] and not b["direction"]:
                # Both undirected — check divided flag, then default NB/SB
                try:
                    ctx = gather_feature_context(bridge_id, lat, lon)
                    # Use crosses for below features, carries for carried-on
                    if loc == "B":
                        is_div, _ = detect_divided(ctx.get("crosses", ""), ctx.get("median_features", []))
                    else:
                        is_div, _ = detect_divided(ctx.get("carries", ""), ctx.get("median_features", []))
                except Exception:
                    is_div = False

                if is_div:
                    # Lower feature_id → NB, higher → SB (inspector should verify)
                    a["direction"] = "NB"
                    b["direction"] = "SB"
                    label = "[DRY RUN] " if args.dry_run else ""
                    print(f"  {label}[FLAG] [{bridge_id}] {a['feature_id']}/{b['feature_id']} "
                          f"both undirected — defaulted NB/SB, please verify orientation.")
                    flagged += 1

        # ── Apply directions and write ────────────────────────────────────────
        label = "[DRY RUN] " if args.dry_run else ""
        for entry in pass1:
            new_name = _directed(entry["new_base"], entry["direction"]) if entry["direction"] else entry["new_base"]
            if new_name == entry["existing_gis"]:
                continue
            print(f"  {label}[{bridge_id}] {entry['feature_id']} B.F.03: '{entry['current']}' → '{new_name}'")
            if not args.dry_run:
                conn.execute(
                    "UPDATE evidence SET gis_value=?, updated_at=datetime('now') WHERE id=?",
                    (new_name, entry["row_id"]),
                )
            updated += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  Bridges with no coords        : {no_coords}")
    print(f"  B.F.03 rows updated           : {updated}")
    print(f"  Undirected pairs auto-flagged : {flagged}")
    if args.dry_run:
        print("\n  Dry run — run without --dry-run to apply.")
    else:
        print("\n  Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Phase 11: AI feature discovery from GIS data"
    )
    parser.add_argument("--dry-run",     action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--bridge",      metavar="ID",
                        help="Process a single bridge ID")
    parser.add_argument("--limit",       type=int, default=None,
                        help="Process first N bridges only")
    parser.add_argument("--status",      metavar="STATUS",
                        help="Filter by bridge status (e.g. BRM_DONE, PLANS_DONE)")
    parser.add_argument("--verbose",     action="store_true",
                        help="Print details for all bridges including no-change ones")
    parser.add_argument("--debug",       action="store_true",
                        help="Print full prompt and raw Claude response for each bridge")
    parser.add_argument("--backfill",    action="store_true",
                        help="Backfill missing type-specific items for existing H*/R*/W* features")
    parser.add_argument("--rename-f03",      action="store_true",
                        help="Update B.F.03 gis_values using GIS-derived names (no API call)")
    parser.add_argument("--reseed-divided",  action="store_true",
                        help="Reseed bridges with divided highways missing an H feature (uses Claude API)")
    args = parser.parse_args()
    if args.backfill:
        run_backfill(args)
    elif args.rename_f03:
        run_rename_f03(args)
    elif args.reseed_divided:
        run_reseed_divided(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
