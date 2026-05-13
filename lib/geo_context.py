"""
geo_context.py — Query ODOT TransGIS ArcGIS REST API for features near a bridge.

Uses official Oregon DOT data instead of OpenStreetMap:
  - Layer 101 (Bridges): CARRIES/CROSSES fields for feature identification
  - Layer 164 (All Public Roads): official road names and route numbers

No API key required. Results are cached in-process per coordinates.
Falls back gracefully on any network/parse error.
"""
import urllib.request
import urllib.parse
import json

ODOT_BASE = "https://gis.odot.state.or.us/arcgis1006/rest/services/transgis/catalog/MapServer"
_TIMEOUT  = 12

_cache = {}


def _query_layer(layer_id, lat, lon, radius_m, out_fields, where="1=1"):
    """Run a single ODOT ArcGIS spatial proximity query. Returns list of feature dicts."""
    url = f"{ODOT_BASE}/{layer_id}/query"
    params = {
        "geometry":      f"{lon},{lat}",
        "geometryType":  "esriGeometryPoint",
        "inSR":          "4326",
        "distance":      str(radius_m),
        "units":         "esriSRUnit_Meter",
        "where":         where,
        "outFields":     out_fields,
        "returnGeometry": "false",
        "f":             "json",
    }
    qs  = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"User-Agent": "SNBI-Pipeline/1.0 (bridge inspection data tool)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return data.get("features", [])
    except Exception:
        return []


def get_nearby_features(lat, lon, radius_m=400):
    """
    Query ODOT TransGIS for bridges and roads within radius_m metres.

    Returns dict:
      "bridges" — list of ODOT bridge attribute dicts (BRIDGE_ID, CARRIES, CROSSES, …)
      "roads"   — set of de-duplicated road label strings
    Returns None on coordinate error.
    """
    if not lat or not lon:
        return None
    try:
        flat, flon = round(float(lat), 4), round(float(lon), 4)
    except (ValueError, TypeError):
        return None

    cache_key = (flat, flon, radius_m)
    if cache_key in _cache:
        return _cache[cache_key]

    # ── Bridges ──────────────────────────────────────────────────────────────
    bridge_feats = _query_layer(
        101, flat, flon, radius_m,
        "BRIDGE_ID,BRIDGE_NAM,CARRIES,CROSSES,YEAR,CNTY_NAME,STRUC_TYP,MATERIAL,DESIGN"
    )
    bridges = []
    for feat in bridge_feats[:10]:
        a = feat.get("attributes", {})
        entry = {k: (a.get(k) or "").strip() for k in
                 ("BRIDGE_ID", "BRIDGE_NAM", "CARRIES", "CROSSES",
                  "YEAR", "CNTY_NAME", "STRUC_TYP", "MATERIAL", "DESIGN")}
        if entry["BRIDGE_ID"]:
            bridges.append(entry)

    # ── Roads (smaller radius — avoid distant arterials) ────────────────────
    road_feats = _query_layer(
        164, flat, flon, min(radius_m, 250),
        "NAME,PREFIX,TYPE,ALIAS_NAME,HWYNUMB,ROADOWNER"
    )
    road_labels = set()
    for feat in road_feats:
        a      = feat.get("attributes", {})
        prefix = (a.get("PREFIX")     or "").strip()
        name   = (a.get("NAME")       or "").strip()
        rtype  = (a.get("TYPE")       or "").strip()
        alias  = (a.get("ALIAS_NAME") or "").strip()
        hwy    = (a.get("HWYNUMB")    or "").strip().lstrip("0")

        label = " ".join(filter(None, [prefix, name, rtype])).strip()
        if alias and alias not in label:
            label = f"{label} ({alias})" if label else alias
        if hwy and f"HWY {hwy}" not in label and alias != f"OR-{hwy}":
            label = f"{label} [HWY {hwy}]" if label else f"HWY {hwy}"
        if label:
            road_labels.add(label)

    result = {"bridges": bridges, "roads": sorted(road_labels)[:12]}
    _cache[cache_key] = result
    return result


def build_context_block(lat, lon):
    """
    Return a formatted text block to append to Claude extraction prompts,
    or "" if coordinates are missing or queries fail.
    """
    data = get_nearby_features(lat, lon)
    if not data:
        return ""

    bridges = data["bridges"]
    roads   = data["roads"]

    if not bridges and not roads:
        return ""

    lines = [
        "\n\nODOT GIS CONTEXT — official Oregon DOT data for this location "
        "(use to identify feature names when plans are ambiguous or illegible):",
        f"  Bridge coordinates: {lat}, {lon}",
    ]

    if bridges:
        lines.append("  Nearby bridges in ODOT inventory:")
        for b in bridges:
            parts = []
            if b["BRIDGE_ID"]:   parts.append(f"ID={b['BRIDGE_ID']}")
            if b["BRIDGE_NAM"]:  parts.append(f'"{b["BRIDGE_NAM"]}"')
            if b["CARRIES"]:     parts.append(f"carries={b['CARRIES']}")
            if b["CROSSES"]:     parts.append(f"crosses={b['CROSSES']}")
            if b["YEAR"]:        parts.append(f"yr={b['YEAR']}")
            if b["CNTY_NAME"]:   parts.append(f"county={b['CNTY_NAME']}")
            lines.append(f"    • {' | '.join(parts)}")

    if roads:
        lines.append("  Nearby roads (ODOT road network):")
        lines.append(f"    {', '.join(roads)}")

    # Travel lane counts from ODOT road inventory — authoritative for B.H.08
    lane_counts = query_lane_counts(lat, lon, radius_m=75)
    if lane_counts:
        # Group by hwynumb; take max lanes per route (mainline > ramps)
        by_route = {}
        for lc in lane_counts:
            key = lc["hwynumb"] or "(local)"
            by_route[key] = max(by_route.get(key, 0), lc["no_lanes"])
        summaries = []
        for route, n in sorted(by_route.items()):
            label = f"Hwy {route}" if route != "(local)" else "Local road"
            summaries.append(f"{label}: {n} lanes")
        lines.append(
            "  ODOT travel lane counts (authoritative for B.H.08 — use these values, "
            "do not infer from roadway width): " + "; ".join(summaries)
        )

    lines.append(
        "  Use CARRIES/CROSSES fields to identify the facility carried and "
        "feature intersected. Match bridge IDs against the bridge_id being processed."
    )
    return "\n".join(lines)


def query_lane_counts(lat, lon, radius_m=75, hwynumb=None, sfx=None):
    """
    Query ODOT TransGIS layers 126 (state roads) and 347 (non-state roads) for
    travel lane counts at a bridge location.

    Returns list of {"no_lanes": int, "hwynumb": str, "roadway_id": str|None} dicts.

    Route-specific mode (hwynumb provided):
        Queries layer 126 filtered by HWYNUMB, optionally also by ST_HWY_SFX.
        Uses a wider radius (max of radius_m and 200m) to find the route even if
        the bridge point is offset from the road centerline.
        No CONN filter — the caller already targets a specific route/suffix, so
        connector segments for that route are correct (e.g., a bridge carrying
        connector DF should get connector-DF lane counts).
        hwynumb must be the zero-stripped integer string (e.g., "64" not "064");
        zero-padding to 3 digits is applied internally for the WHERE clause.
        sfx is the ST_HWY_SFX value from HWY_AND_CON (e.g., "DF", "00").

    Spatial-only mode (no hwynumb):
        Falls back to a proximity query on layers 126 + 347.
        Connector/ramp segments (HWYNAME contains "CONN") are excluded because
        in the absence of route context a spatial query near an interchange may
        pick up ramps that are not the carried feature.

    Deduplication key is (lanes, hwynumb, roadway_id) so that directional
    carriageways of a divided highway are kept as separate entries.
    """
    results = []
    seen    = set()

    if hwynumb:
        # ── Route-specific mode ───────────────────────────────────────────────
        hwynumb_padded = hwynumb.zfill(3)
        where = f"HWYNUMB='{hwynumb_padded}'"
        if sfx:
            where += f" AND ST_HWY_SFX='{sfx.upper()}'"
        effective_r = max(radius_m, 200)
        for feat in _query_layer(126, lat, lon, effective_r,
                                 "NO_LANES,HWYNUMB,RDWY_ID,HWYNAME", where=where):
            a = feat.get("attributes", {})
            try:
                lanes = int(a.get("NO_LANES") or 0)
            except (ValueError, TypeError):
                continue
            if lanes <= 0:
                continue
            hwy = (a.get("HWYNUMB") or "").strip().lstrip("0")
            rid = a.get("RDWY_ID")
            key = (lanes, hwy, rid)
            if key not in seen:
                seen.add(key)
                results.append({"no_lanes": lanes, "hwynumb": hwy, "roadway_id": rid})
    else:
        # ── Spatial-only mode ─────────────────────────────────────────────────
        for feat in _query_layer(126, lat, lon, radius_m, "NO_LANES,HWYNUMB,RDWY_ID,HWYNAME"):
            a = feat.get("attributes", {})
            try:
                lanes = int(a.get("NO_LANES") or 0)
            except (ValueError, TypeError):
                continue
            if lanes <= 0:
                continue
            hwyname = (a.get("HWYNAME") or "").upper()
            if "CONN" in hwyname:
                continue
            hwy = (a.get("HWYNUMB") or "").strip().lstrip("0")
            rid = a.get("RDWY_ID")
            key = (lanes, hwy, rid)
            if key not in seen:
                seen.add(key)
                results.append({"no_lanes": lanes, "hwynumb": hwy, "roadway_id": rid})

        # Layer 347 — Non-state roads (only in spatial-only mode)
        for feat in _query_layer(347, lat, lon, radius_m, "NO_LANES"):
            a = feat.get("attributes", {})
            try:
                lanes = int(a.get("NO_LANES") or 0)
            except (ValueError, TypeError):
                continue
            if lanes <= 0:
                continue
            key = (lanes, "", None)
            if key not in seen:
                seen.add(key)
                results.append({"no_lanes": lanes, "hwynumb": "", "roadway_id": None})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# B.F.03 name builder
# ═══════════════════════════════════════════════════════════════════════════

def _rte_priority(r):
    """Sort key: Interstate < US < State < other (most recognizable first)."""
    r = r.upper()
    if r.startswith("I-") or r.startswith("I "):    return 0
    if r.startswith("US-") or r.startswith("US "):   return 1
    if r.startswith("OR-") or r.startswith("OR ") or r.startswith("SR-"): return 2
    return 3


def _is_route_desig(part):
    """Return True if a B.F.03 pipe segment looks like a route designation vs. a common name."""
    p = part.strip().upper()
    return (p.startswith("I-") or p.startswith("I ")
            or p.startswith("US-") or p.startswith("US ")
            or p.startswith("OR-") or p.startswith("OR ")
            or p.startswith("SR-") or p.startswith("CR-")
            or bool(_re.match(r'^[A-Z]{1,3}\s*\d', p)))


def f03_with_direction(base_name, direction):
    """
    Append a direction suffix (NB/SB/EB/WB) to route designations in a pipe-delimited
    B.F.03 base name, leaving the common/official name components unchanged.

    Example: f03_with_direction("I-205|East Portland Freeway", "NB")
             → "I-205 NB|East Portland Freeway"
    """
    if not direction or not base_name:
        return base_name
    parts = [p.strip() for p in base_name.split("|")]
    result = []
    for p in parts:
        result.append(f"{p} {direction}" if _is_route_desig(p) else p)
    return "|".join(result)


_SKIP_ROAD_TYPES = {"FWY", "RAMP", "HWY", "EXPWY"}


def _name_from_layer164(lat, lon, hint_text=None, radius_m=150):
    """
    Find a properly-formatted road name from ODOT layer 164 near (lat, lon).

    Two-pass strategy:
      Pass 1 — CONN entries whose alias road type is a common road type (not RAMP/HWY/FWY):
               build "ALIAS_PREFIX ALIAS_NAME ALIAS_TYPE" → e.g. "SE Johnson Creek Blvd".
      Pass 2 — non-CONN, non-FWY, non-RAMP entries whose name tokens overlap with hint_text
               (if provided): build "PREFIX NAME TYPE" → e.g. "SE Johnson Creek Blvd".

    Prefix-like tokens (SE, NE, SW, NW) are uppercased; other tokens are title-cased.
    Returns "" if no suitable road is found.
    """
    feats = _query_layer(
        164, lat, lon, radius_m,
        "NAME,PREFIX,TYPE,ALIAS_NAME,ALIAS_PREFIX,ALIAS_TYPE",
    )

    _DIR_PREFIXES = {"NE", "NW", "SE", "SW", "N", "S", "E", "W"}

    def _fmt(*parts):
        tokens = " ".join(filter(None, parts)).split()
        return " ".join(t.upper() if t.upper() in _DIR_PREFIXES else t.capitalize()
                        for t in tokens)

    # Pass 1: CONN entries with a useful alias road type
    for feat in feats:
        a = feat.get("attributes", {})
        if (a.get("TYPE") or "").strip().upper() != "CONN":
            continue
        alias_type = (a.get("ALIAS_TYPE") or "").strip().upper()
        if not alias_type or alias_type in _SKIP_ROAD_TYPES:
            continue
        alias_pfx  = (a.get("ALIAS_PREFIX") or "").strip()
        alias_name = (a.get("ALIAS_NAME")   or "").strip()
        if not alias_name:
            continue
        name = _fmt(alias_pfx, alias_name, alias_type)
        if name:
            return name

    # Pass 2: non-CONN, non-FWY/RAMP entries (with optional hint matching)
    hint_tokens = set((hint_text or "").lower().split()) - {"the", "of", "a", "an"}
    for feat in feats:
        a = feat.get("attributes", {})
        ftype  = (a.get("TYPE")   or "").strip().upper()
        prefix = (a.get("PREFIX") or "").strip()
        name   = (a.get("NAME")   or "").strip()
        if not name or ftype in _SKIP_ROAD_TYPES:
            continue
        if hint_tokens:
            label_tokens = set((prefix + " " + name + " " + ftype).lower().split())
            if not (hint_tokens & label_tokens):
                continue
        result = _fmt(prefix, name, ftype)
        if result:
            return result

    return ""


def build_f03_name(hwynumb, lat, lon, fallback_text, radius_m=300, sfx=None):
    """
    Build a proper SNBI B.F.03 base name for a highway feature.

    Queries ODOT layer 166 (signed routes) filtered by HWYNUMB to find:
      - ALL_RTE: signed route designation(s) like "I-205"
      - HWYNAME: official highway name like "EAST PORTLAND FREEWAY"
      - ROAD_NAME: local/common road name if present

    Returns pipe-delimited name with route numbers first (sorted I > US > OR > other),
    then common name, e.g. "I-205|East Portland Freeway".

    Falls back to _name_from_layer164 (then fallback_text) when:
      - hwynumb is None (local/connector road with no highway number in CARRIES)
      - layer 166 returns no features
      - sfx is provided and is not "00" (explicitly a connector/ramp suffix from HWY_AND_CON)
      - all layer 166 results have non-"00" ST_HWY_SFX (detected as connector from context)
    """
    if not hwynumb:
        # No highway number — try layer 164 for a properly-prefixed local/connector road name
        road_164 = _name_from_layer164(lat, lon, hint_text=fallback_text)
        return road_164 or (fallback_text or "").strip()

    # If sfx is explicitly a connector/ramp suffix, skip layer 166 (which would return the
    # parent highway route) and go directly to the layer 164 common name.
    if sfx and sfx != "00":
        road_164 = _name_from_layer164(lat, lon, hint_text=fallback_text)
        return road_164 or (fallback_text or f"Hwy {hwynumb}").strip()

    hwynumb_padded = hwynumb.zfill(3)
    feats = _query_layer(
        166, lat, lon, radius_m,
        "ALL_RTE,HWYNAME,ST_HWY_SFX,ROAD_NAME",
        where=f"HWYNUMB='{hwynumb_padded}'",
    )
    if not feats:
        # Layer 166 returned nothing — try layer 164 before falling back to raw text
        road_164 = _name_from_layer164(lat, lon, hint_text=fallback_text)
        return road_164 or (fallback_text or f"Hwy {hwynumb}").strip()

    # If every layer 166 result is a connector/ramp (no "00" mainline suffix), the HWYNUMB
    # maps to a ramp/connector and the route designation would be the parent freeway name.
    all_connector = all(
        (f.get("attributes", {}).get("ST_HWY_SFX") or "").strip() != "00"
        for f in feats
    )
    if all_connector:
        road_164 = _name_from_layer164(lat, lon, hint_text=fallback_text)
        if road_164:
            return road_164

    route_desigs = []
    hwyname      = ""
    road_name    = ""

    for f in feats:
        a   = f.get("attributes", {})
        rte = (a.get("ALL_RTE") or "").strip()
        if rte and rte not in route_desigs:
            route_desigs.append(rte)
        feat_sfx = (a.get("ST_HWY_SFX") or "").strip()
        if not hwyname and feat_sfx == "00":
            hwyname = " ".join(w.capitalize() for w in
                               (a.get("HWYNAME") or "").strip().split())
        if not road_name:
            rn = " ".join(w.capitalize() for w in
                          (a.get("ROAD_NAME") or "").strip().split())
            if rn:
                road_name = rn

    # Synthesize SR-{N} from HWYNUMB if not already represented in route_desigs
    try:
        sr_num   = str(int(hwynumb))          # "064" → "64"
        sr_desig = f"SR-{sr_num}"
        already_covered = any(
            _re.sub(r'\D', '', r) == sr_num   # exact digit match — avoids SR-5 shadowed by I-205
            for r in route_desigs
        )
        if not already_covered:
            route_desigs.append(sr_desig)
    except (ValueError, TypeError):
        pass

    # Remove any entry whose tokens are fully covered by a longer combined entry.
    # e.g. ["I-405 US-26", "I-405", "US-26"] → ["I-405 US-26"]
    route_desigs = [
        r for r in route_desigs
        if not any(r != c and set(r.split()) <= set(c.split()) for c in route_desigs)
    ]

    route_desigs.sort(key=_rte_priority)

    parts = list(route_desigs)
    # Add official highway name if it adds meaningful context beyond the route designation
    common = road_name or hwyname
    if common and common.upper() not in [p.upper() for p in parts]:
        parts.append(common)

    return "|".join(parts) if parts else (fallback_text or f"Hwy {hwynumb}").strip()


# ═══════════════════════════════════════════════════════════════════════════
# Divided-highway detection helper
# ═══════════════════════════════════════════════════════════════════════════

# MEDN_CD values that confirm a divided highway (anything beyond painted/mountable)
_DIVIDED_MEDN_CODES = {4, 5, 6, 7, 8, 9, 10, 11}

import re as _re

def _hwy_num(text):
    """Extract zero-stripped highway number from ODOT text like 'Hwy 064' or 'Hwy 26'."""
    m = _re.search(r'[Hh][Ww][Yy]\.?\s*0*(\d+)', text or "")
    return m.group(1) if m else None


def detect_divided(carries_or_crosses, median_features):
    """
    Return (is_divided, confirmation_string) by matching the highway number in
    a CARRIES or CROSSES text against layer 377 median_features.

    Prefers mainline segments (ST_HWY_SFX='00') over connectors/ramps.
    Uses MEDN_CD >= 4 as the divided threshold (anything beyond painted/mountable).
    Returns (False, None) if no match or median code is below threshold.
    """
    num = _hwy_num(carries_or_crosses)
    if not num:
        return False, None

    mainline = None
    any_match = None
    for mf in median_features:
        if mf["hwynumb"].lstrip("0") != num:
            continue
        if mf["medn_cd"] not in _DIVIDED_MEDN_CODES:
            continue
        if mf["sfx"] == "00":
            mainline = mf
            break
        if any_match is None:
            any_match = mf

    best = mainline or any_match
    if not best:
        return False, None

    road  = best["hwyname"] or f"Hwy {num}"
    mtype = best["desc"] or f"MEDN_CD={best['medn_cd']}"
    return True, f"{mtype} median on {road} (ODOT layer 377)"


# ═══════════════════════════════════════════════════════════════════════════
# Feature Discovery — used by 09_discover_features.py
# ═══════════════════════════════════════════════════════════════════════════

# Layer 377 median codes that indicate a non-mountable (physical) median → divided highway
DIVIDED_MEDIAN_CODES = {2, 8, 9, 11}  # Mountable=2, Curbed=8, Vegetation=9, Barrier=11


def _query_by_attr(layer_id, where, out_fields, return_geometry=False):
    """Non-spatial attribute query against ODOT TransGIS. Returns list of feature dicts."""
    url    = f"{ODOT_BASE}/{layer_id}/query"
    params = {
        "where":          where,
        "outFields":      out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "f":              "json",
    }
    if return_geometry:
        params["outSR"] = "4326"
    qs  = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"User-Agent": "SNBI-Pipeline/1.0 (bridge inspection data tool)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return data.get("features", [])
    except Exception:
        return []


def get_bridge_coords(bridge_id):
    """
    Query layer 101 by BRIDGE_ID attribute to get coordinates in WGS84.
    Returns (lat, lon) or (None, None) on failure.
    """
    feats = _query_by_attr(
        101,
        f"BRIDGE_ID='{bridge_id}'",
        "BRIDGE_ID",
        return_geometry=True,
    )
    if not feats:
        return None, None
    geom = feats[0].get("geometry", {})
    if "x" in geom and "y" in geom:          # point layer
        lon, lat = geom["x"], geom["y"]
    elif "rings" in geom and geom["rings"]:   # polygon — take centroid of first ring
        ring = geom["rings"][0]
        lon  = sum(p[0] for p in ring) / len(ring)
        lat  = sum(p[1] for p in ring) / len(ring)
    else:
        return None, None
    return round(float(lat), 6), round(float(lon), 6)


def _query_osm_waterway(lat, lon, radius_m=200):
    """Return the most prominent named waterway from OSM Overpass near (lat, lon), or None."""
    query = (
        f"[out:json][timeout:15];"
        f"(way[\"waterway\"][\"name\"](around:{radius_m},{lat},{lon});"
        f"relation[\"waterway\"][\"name\"](around:{radius_m},{lat},{lon}););"
        f"out tags;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    req  = urllib.request.Request(
        "https://overpass-api.de/api/interpreter",
        data=data,
        headers={"User-Agent": "SNBI-Pipeline/1.0 (bridge inspection data tool)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        elements = result.get("elements", [])
        # Prefer more significant waterway types
        priority = {"river": 0, "stream": 1, "canal": 2, "drain": 3}
        named = sorted(
            (priority.get(e["tags"].get("waterway", ""), 99), e["tags"]["name"])
            for e in elements
            if e.get("tags", {}).get("name")
        )
        return named[0][1] if named else None
    except Exception:
        return None


def _rail_service_type(records):
    """
    Given list of (CARGO, STATUS) tuples for one railroad, return B.RR.01 code.
    F=Freight, P=Passenger, M=Mixed, I=Inactive.
    """
    active = [
        c.upper() for c, s in records
        if "ACTIVE" in s.upper() and "INACTIVE" not in s.upper()
    ]
    if not active:
        return "I"
    has_freight   = any("FREIGHT"   in c for c in active)
    has_passenger = any("PASSENGER" in c for c in active)
    if has_freight and has_passenger:
        return "M"
    return "P" if has_passenger else "F"


def discover_features(lat, lon, feature_category=None):
    """
    Query ODOT TransGIS and OSM for feature enrichment data near a bridge.

    Args:
        lat, lon          -- bridge coordinates (WGS84 decimal degrees)
        feature_category  -- "HIGHWAY" | "RAILROAD" | "WATERWAY" | None

    Returns dict:
        h_name              str       -- B.F.03 for H* features ("OR-22|Pacific Highway East")
        rail_lines          list      -- [{name, abbr, service_type}, ...]  from layer 143
        waterway_name       str|None  -- named waterway from OSM (only if WATERWAY category)
        divided_median_code int|None  -- MEDN_CD if non-mountable median found, else None
        has_sidewalk        bool      -- layer 132 hit within 150m
        sidewalk_desc       str       -- "Sidewalk" or "Left and right sidewalks"
        has_bicycle         bool      -- layer 136 hit within 150m
        bicycle_desc        str       -- "Shared Use Path" / "Bicycle Lane" / "Bicycle Facility"
    """
    result = {
        "h_name":               "",
        "rail_lines":           [],
        "waterway_name":        None,
        "divided_median_code":  None,
        "has_sidewalk":         False,
        "sidewalk_desc":        "",
        "has_bicycle":          False,
        "bicycle_desc":         "",
    }

    # ── Layer 166: Signed Routes → route designations for H* B.F.03 ─────
    route_names = []
    for f in _query_layer(166, lat, lon, 200, "ALL_RTE"):
        rte = (f.get("attributes", {}).get("ALL_RTE") or "").strip()
        if rte and rte not in route_names:
            route_names.append(rte)

    # ── Layer 164: All Public Roads → plain road name ────────────────────
    road_name = ""
    for f in _query_layer(164, lat, lon, 150, "NAME,TYPE"):
        a = f.get("attributes", {})
        n = (a.get("NAME") or "").strip()
        t = (a.get("TYPE") or "").strip()
        candidate = " ".join(filter(None, [n, t]))
        if candidate:
            road_name = candidate
            break

    # Build H* name: "OR-22 US-20|Pacific Highway East"
    parts = []
    if route_names:
        parts.append(" ".join(route_names[:3]))
    if road_name:
        parts.append(road_name)
    result["h_name"] = "|".join(parts)

    # ── Layer 143: Rail Network ──────────────────────────────────────────
    rr_groups = {}
    for f in _query_layer(143, lat, lon, 300, "RR_NAME,RR_ABBR,CARGO,STATUS"):
        a    = f.get("attributes", {})
        abbr = (a.get("RR_ABBR") or "").strip()
        name = (a.get("RR_NAME") or "").strip()
        if not abbr:
            continue
        rr_groups.setdefault(abbr, {"name": name, "records": []})
        cargo  = (a.get("CARGO")  or "").strip()
        status = (a.get("STATUS") or "").strip()
        rr_groups[abbr]["records"].append((cargo, status))

    result["rail_lines"] = [
        {"name": info["name"], "abbr": abbr,
         "service_type": _rail_service_type(info["records"])}
        for abbr, info in rr_groups.items()
    ]

    # ── OSM: Waterway name (only for waterway-category bridges) ──────────
    if feature_category == "WATERWAY":
        result["waterway_name"] = _query_osm_waterway(lat, lon)

    # ── Layer 377: Medians → divided highway detection ───────────────────
    for f in _query_layer(377, lat, lon, 100, "MEDN_CD"):
        code = f.get("attributes", {}).get("MEDN_CD")
        try:
            if int(code) in DIVIDED_MEDIAN_CODES:
                result["divided_median_code"] = int(code)
                break
        except (ValueError, TypeError):
            pass

    # ── Layer 132: Sidewalk ──────────────────────────────────────────────
    sides = set()
    for f in _query_layer(132, lat, lon, 150, "ROADSIDE,WD_MEAS"):
        side = (f.get("attributes", {}).get("ROADSIDE") or "").strip().upper()
        if side:
            sides.add(side)
    if sides:
        result["has_sidewalk"] = True
        result["sidewalk_desc"] = (
            "Left and right sidewalks" if len(sides) > 1 else "Sidewalk"
        )

    # ── Layer 136: Bicycle Facilities ────────────────────────────────────
    bike_types = set()
    for f in _query_layer(136, lat, lon, 150, "ROADSIDE,TYP_CD,WD_MEAS"):
        tcd = (f.get("attributes", {}).get("TYP_CD") or "").strip()
        if tcd:
            bike_types.add(tcd)
    if bike_types:
        result["has_bicycle"] = True
        if any("SHARED" in t or "PATH" in t for t in bike_types):
            result["bicycle_desc"] = "Shared Use Path"
        elif any("LANE" in t for t in bike_types):
            result["bicycle_desc"] = "Bicycle Lane"
        else:
            result["bicycle_desc"] = "Bicycle Facility"

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Phase 11 — comprehensive GIS context for AI feature discovery
# ═══════════════════════════════════════════════════════════════════════════

def get_bridge_carries_crosses(bridge_id):
    """
    Query layer 101 for the CARRIES, CROSSES, and HWY_AND_CON fields of a specific bridge.
    Returns (carries_str, crosses_str, hwy_and_con_str); empty strings on failure.

    HWY_AND_CON encodes the carried highway and suffix (e.g. "064DF" = Hwy 064, connector DF;
    "06400" = Hwy 064 mainline).  Use _parse_hwy_and_con() to split it.
    """
    feats = _query_by_attr(
        101,
        f"BRIDGE_ID='{bridge_id}'",
        "BRIDGE_ID,CARRIES,CROSSES,HWY_AND_CON",
        return_geometry=False,
    )
    if not feats:
        return "", "", ""
    a = feats[0].get("attributes", {})
    return (
        (a.get("CARRIES")     or "").strip(),
        (a.get("CROSSES")     or "").strip(),
        (a.get("HWY_AND_CON") or "").strip(),
    )


def _parse_hwy_and_con(hwy_and_con):
    """
    Parse ODOT HWY_AND_CON field into (hwynumb, sfx).

    Examples:
        "064DF" → ("64", "DF")   connector DF of highway 64
        "06400" → ("64", "00")   mainline of highway 64
        "02600" → ("26", "00")

    Returns ("", "") on empty or unparseable input.
    """
    if not hwy_and_con:
        return "", ""
    m = _re.match(r'^(\d{3})(.{0,2})\s*$', hwy_and_con.strip())
    if not m:
        return "", ""
    num = m.group(1).lstrip("0") or "0"
    sfx = (m.group(2) or "00").strip().upper() or "00"
    return num, sfx


def find_nearby_bridges(bridge_id, lat, lon, radius_m=200):
    """
    Query ODOT layer 101 for bridges within radius_m meters of (lat, lon).
    Excludes the current bridge_id.
    Returns list of {"bridge_id": ..., "carries": ..., "crosses": ...} dicts.
    """
    feats = _query_layer(101, lat, lon, radius_m, "BRIDGE_ID,CARRIES,CROSSES")
    result = []
    for feat in feats:
        a   = feat.get("attributes", {})
        bid = (a.get("BRIDGE_ID") or "").strip()
        if bid and bid != bridge_id:
            result.append({
                "bridge_id": bid,
                "carries":   (a.get("CARRIES") or "").strip(),
                "crosses":   (a.get("CROSSES") or "").strip(),
            })
    return result


def _query_osm_all_features(lat, lon, radius_m=150):
    """
    Query OSM Overpass for highway, railway, and waterway ways near a bridge.
    Returns deduplicated list of tag dicts; empty list on failure.
    Key tags returned: name, ref, highway, railway, waterway, bridge, tunnel, layer, oneway.
    """
    query = (
        f"[out:json][timeout:25];"
        f"("
        f"way[highway](around:{radius_m},{lat},{lon});"
        f"way[railway](around:{radius_m},{lat},{lon});"
        f"way[waterway][name](around:{radius_m},{lat},{lon});"
        f");"
        f"out tags;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    req  = urllib.request.Request(
        "https://overpass-api.de/api/interpreter",
        data=data,
        headers={"User-Agent": "SNBI-Pipeline/1.0 (bridge inspection data tool)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        seen     = set()
        features = []
        for elem in result.get("elements", []):
            tags = elem.get("tags", {})
            key  = (
                tags.get("name", "") or tags.get("ref", ""),
                tags.get("highway", "") or tags.get("railway", "") or tags.get("waterway", ""),
                tags.get("bridge", ""),
                tags.get("layer",  "0"),
            )
            if key in seen:
                continue
            seen.add(key)
            features.append({
                "name":     tags.get("name",     ""),
                "ref":      tags.get("ref",      ""),
                "highway":  tags.get("highway",  ""),
                "railway":  tags.get("railway",  ""),
                "waterway": tags.get("waterway", ""),
                "bridge":   tags.get("bridge",   ""),
                "tunnel":   tags.get("tunnel",   ""),
                "layer":    tags.get("layer",    "0"),
                "oneway":   tags.get("oneway",   ""),
            })
        return features
    except Exception:
        return []


def gather_feature_context(bridge_id, lat, lon):
    """
    Gather comprehensive GIS context for Phase 11 AI feature discovery.

    Queries ODOT TransGIS (layers 101, 143, 132, 136, 164, 166, 377) and OSM
    and returns a structured dict for prompt construction. Falls back gracefully
    on any network error.
    """
    carries, crosses, hwy_and_con = get_bridge_carries_crosses(bridge_id)

    # Parse HWY_AND_CON for route-specific lane count lookup and connector detection.
    # hac_hwynumb/hac_sfx are the definitive highway number + suffix for the carried route
    # (e.g. hwy_and_con="064DF" → hwynumb="64", sfx="DF" for connector bridges).
    hac_hwynumb, hac_sfx = _parse_hwy_and_con(hwy_and_con)

    route_names = []
    for f in _query_layer(166, lat, lon, 100, "ALL_RTE"):
        rte = (f.get("attributes", {}).get("ALL_RTE") or "").strip()
        if rte and rte not in route_names:
            route_names.append(rte)

    roads = []
    for f in _query_layer(164, lat, lon, 200, "NAME,PREFIX,TYPE,HWYNUMB"):
        a     = f.get("attributes", {})
        name  = " ".join(filter(None, [
            (a.get("PREFIX") or "").strip(),
            (a.get("NAME")   or "").strip(),
            (a.get("TYPE")   or "").strip(),
        ])).strip()
        hwy   = (a.get("HWYNUMB") or "").strip().lstrip("0")
        if name:
            label = f"{name} [HWY {hwy}]" if hwy else name
            if label not in roads:
                roads.append(label)

    # Collect all median features with road names so build_prompt can match against CARRIES/CROSSES
    median_features = []
    seen_hwy = set()
    for f in _query_layer(377, lat, lon, 150, "MEDN_CD,HWYNAME,HWYNUMB,ST_HWY_SFX,CODE_DESC"):
        a = f.get("attributes", {})
        try:
            code = int(a.get("MEDN_CD"))
        except (ValueError, TypeError):
            continue
        hwyname = (a.get("HWYNAME") or "").strip()
        hwynumb = (a.get("HWYNUMB") or "").strip().lstrip("0")
        sfx     = (a.get("ST_HWY_SFX") or "").strip()
        desc    = (a.get("CODE_DESC") or "").strip()
        key = f"{hwynumb}:{sfx}"
        if key not in seen_hwy:
            seen_hwy.add(key)
            median_features.append({
                "hwyname":  hwyname,
                "hwynumb":  hwynumb,
                "sfx":      sfx,
                "medn_cd":  code,
                "desc":     desc,
            })
    # Keep backward-compat field for anything that still reads median_code
    median_code = median_features[0]["medn_cd"] if median_features else None

    # Pre-compute divided-highway determination so build_prompt doesn't rely on Claude guessing
    carries_divided, carries_divided_reason = detect_divided(carries, median_features)
    crosses_divided, crosses_divided_reason = detect_divided(crosses, median_features)

    # Pre-build B.F.03 base names using signed-route data (most recognizable name first).
    # Pass hac_sfx so build_f03_name can skip layer 166 for connector routes (sfx != "00"),
    # which would otherwise return the parent freeway designation instead of the connector name.
    carries_hwynumb  = _hwy_num(carries) or hac_hwynumb
    crosses_hwynumb  = _hwy_num(crosses)
    carries_f03_base = build_f03_name(carries_hwynumb, lat, lon, carries, sfx=hac_sfx or None)
    crosses_f03_base = build_f03_name(crosses_hwynumb, lat, lon, crosses)

    sidewalk_sides = set()
    for f in _query_layer(132, lat, lon, 150, "ROADSIDE"):
        side = (f.get("attributes", {}).get("ROADSIDE") or "").strip()
        if side:
            sidewalk_sides.add(side)

    bike_types = set()
    for f in _query_layer(136, lat, lon, 150, "TYP_CD"):
        t = (f.get("attributes", {}).get("TYP_CD") or "").strip()
        if t:
            bike_types.add(t)

    rail_names = []
    for f in _query_layer(143, lat, lon, 300, "RR_NAME,RR_ABBR"):
        a    = f.get("attributes", {})
        abbr = (a.get("RR_ABBR") or "").strip()
        name = (a.get("RR_NAME") or "").strip()
        key  = abbr or name
        if key and key not in rail_names:
            rail_names.append(key)

    osm_features = _query_osm_all_features(lat, lon)

    # Route-specific lane count query using HWY_AND_CON.
    # When hac_hwynumb and hac_sfx are known, queries layer 126 filtered by HWYNUMB+ST_HWY_SFX
    # so only the carried route's segments are returned (no ramp/connector contamination for
    # mainline bridges; correct connector segments for connector-carrying bridges).
    lane_counts = query_lane_counts(
        lat, lon,
        hwynumb=hac_hwynumb or None,
        sfx=hac_sfx or None,
    )

    return {
        "bridge_id":              bridge_id,
        "carries":                carries,
        "crosses":                crosses,
        "carries_f03_base":       carries_f03_base,
        "crosses_f03_base":       crosses_f03_base,
        "route_names":            route_names,
        "roads":                  roads[:10],
        "median_code":            median_code,
        "median_features":        median_features,
        "carries_divided":        carries_divided,
        "carries_divided_reason": carries_divided_reason,
        "crosses_divided":        crosses_divided,
        "crosses_divided_reason": crosses_divided_reason,
        "sidewalk_sides":         sorted(sidewalk_sides),
        "bike_types":             sorted(bike_types),
        "rail_names":             rail_names,
        "osm_features":   osm_features,
        "lane_counts":    lane_counts,
    }
