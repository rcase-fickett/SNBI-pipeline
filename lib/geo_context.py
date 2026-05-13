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

    lines.append(
        "  Use CARRIES/CROSSES fields to identify the facility carried and "
        "feature intersected. Match bridge IDs against the bridge_id being processed."
    )
    return "\n".join(lines)


def query_lane_counts(lat, lon, radius_m=75):
    """
    Query ODOT TransGIS layers 126 (state roads) and 347 (non-state roads) for
    travel lane counts at a bridge location.

    Returns list of {"no_lanes": int, "hwynumb": str, "roadway_id": str|None} dicts,
    state-road results first (layer 126 has HWYNUMB and RDWY_ID for per-carriageway
    matching), then non-state (layer 347, roadway_id=None).

    Connector/ramp segments (HWYNAME contains "CONN") are excluded — they represent
    interchange connectors, not mainline highway lanes.

    Deduplication key is (lanes, hwynumb, roadway_id) so that directional carriageways
    of a divided highway (same HWYNUMB, different RDWY_ID, different lane counts) are
    kept as separate entries rather than collapsed into one.
    """
    results = []
    seen    = set()

    # Layer 126 — State highways; has HWYNUMB and RDWY_ID for route/direction matching
    for feat in _query_layer(126, lat, lon, radius_m, "NO_LANES,HWYNUMB,RDWY_ID,HWYNAME"):
        a = feat.get("attributes", {})
        try:
            lanes = int(a.get("NO_LANES") or 0)
        except (ValueError, TypeError):
            continue
        if lanes <= 0:
            continue
        # Skip connector/ramp segments — not mainline highway lanes
        hwyname = (a.get("HWYNAME") or "").upper()
        if "CONN" in hwyname:
            continue
        hwy = (a.get("HWYNUMB") or "").strip().lstrip("0")
        rid = a.get("RDWY_ID")  # string e.g. '1' or '2'; None if absent
        key = (lanes, hwy, rid)
        if key not in seen:
            seen.add(key)
            results.append({"no_lanes": lanes, "hwynumb": hwy, "roadway_id": rid})

    # Layer 347 — Non-state roads; no HWYNUMB or RDWY_ID
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
    Query layer 101 for the CARRIES and CROSSES text fields of a specific bridge.
    Returns (carries_str, crosses_str); empty strings on failure.
    """
    feats = _query_by_attr(
        101,
        f"BRIDGE_ID='{bridge_id}'",
        "BRIDGE_ID,CARRIES,CROSSES",
        return_geometry=False,
    )
    if not feats:
        return "", ""
    a = feats[0].get("attributes", {})
    return (a.get("CARRIES") or "").strip(), (a.get("CROSSES") or "").strip()


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
    carries, crosses = get_bridge_carries_crosses(bridge_id)

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

    median_code = None
    for f in _query_layer(377, lat, lon, 100, "MEDN_CD"):
        try:
            median_code = int(f.get("attributes", {}).get("MEDN_CD"))
            break
        except (ValueError, TypeError):
            pass

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

    return {
        "bridge_id":      bridge_id,
        "carries":        carries,
        "crosses":        crosses,
        "route_names":    route_names,
        "roads":          roads[:10],
        "median_code":    median_code,
        "sidewalk_sides": sorted(sidewalk_sides),
        "bike_types":     sorted(bike_types),
        "rail_names":     rail_names,
        "osm_features":   osm_features,
    }
