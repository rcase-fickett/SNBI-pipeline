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
