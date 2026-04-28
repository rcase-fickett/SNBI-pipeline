"""
results_merger.py — Merges Claude API extraction results into the evidence table.
"""
try:
    from lib.db import upsert_evidence
    from lib.snbi_items import ITEM_BY_ID
except ImportError:
    from db import upsert_evidence
    from snbi_items import ITEM_BY_ID


def merge_plan_result(conn, bridge_id, result, page_info):
    """Merge results from a PLAN page into evidence table."""
    source_tag = f"Dwg {page_info.get('drawing_number','')} ({page_info.get('doc_type_str','')} {page_info.get('year','')})"

    _merge_item(conn, bridge_id, "PRIMARY", result, "B.G.01", source_tag)
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.G.05", source_tag)
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.G.13", source_tag)
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.G.12", source_tag)
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.G.14", source_tag)
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.W.01", source_tag)

    # Features
    for feat in result.get("features", []):
        fid = feat.get("feature_id")
        if not fid:
            continue
        for item_id, val in [
            ("B.F.02", feat.get("location")),
            ("B.F.03", feat.get("name")),
        ]:
            if val:
                _write_evidence(conn, bridge_id, item_id, fid, val, "HIGH", source_tag, None)

    # Work events
    for evt in result.get("work_events", []):
        yr = evt.get("year")
        codes = evt.get("work_codes", [])
        reasoning = evt.get("reasoning", "")
        if yr and codes:
            fid = f"WORK:{yr}"
            _write_evidence(conn, bridge_id, "B.W.02", fid, str(yr), "HIGH", source_tag, reasoning)
            _write_evidence(conn, bridge_id, "B.W.03", fid, "|".join(codes), "HIGH", source_tag, reasoning)

    # Questions
    questions = result.get("questions", [])
    if questions:
        q_text = " | ".join(questions)
        # Append to existing questions in the first PRIMARY item
        _append_question(conn, bridge_id, "B.G.01", "PRIMARY", q_text)


def merge_section_result(conn, bridge_id, result, page_info):
    source_tag = f"Dwg {page_info.get('drawing_number','')} ({page_info.get('doc_type_str','')} {page_info.get('year','')})"

    for item_id in ("B.G.05","B.G.07","B.G.08","B.G.10","B.H.16"):
        _merge_item(conn, bridge_id, "PRIMARY", result, item_id, source_tag)

    # B.H.08 and B.H.16 are per highway feature
    if "B.H.08" in result and result["B.H.08"].get("value") is not None:
        r = result["B.H.08"]
        _write_evidence(conn, bridge_id, "B.H.08", "H01",
                        r["value"], r.get("confidence","HIGH"),
                        source_tag, r.get("reasoning"))

    if "B.H.16" in result and result["B.H.16"].get("value") is not None:
        r = result["B.H.16"]
        _write_evidence(conn, bridge_id, "B.H.16", "H01",
                        r["value"], r.get("confidence","HIGH"),
                        source_tag, r.get("reasoning"))

    questions = result.get("questions", [])
    if questions:
        _append_question(conn, bridge_id, "B.G.07", "PRIMARY", " | ".join(questions))


def merge_rail_result(conn, bridge_id, result, page_info):
    source_tag = f"Dwg {page_info.get('drawing_number','')} ({page_info.get('doc_type_str','')} {page_info.get('year','')})"
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.RH.01", source_tag)
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.RH.02", source_tag)


def merge_notes_result(conn, bridge_id, result, page_info):
    source_tag = f"Dwg {page_info.get('drawing_number','')} ({page_info.get('doc_type_str','')} {page_info.get('year','')})"
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.W.01", source_tag)

    # Railroad service type
    rr = result.get("railroad_service_type", {})
    if isinstance(rr, dict) and rr.get("value"):
        _write_evidence(conn, bridge_id, "B.RR.01", "R01",
                        rr["value"], "HIGH", source_tag, rr.get("reasoning"))

    # Navigability clue
    nav = result.get("navigability_clue", {})
    if isinstance(nav, dict) and nav.get("value"):
        _write_evidence(conn, bridge_id, "B.N.01", "W01",
                        nav["value"], "APPROX", source_tag, nav.get("reasoning"))

    for evt in result.get("work_events", []):
        yr = evt.get("year")
        codes = evt.get("work_codes", [])
        if yr and codes:
            fid = f"WORK:{yr}"
            _write_evidence(conn, bridge_id, "B.W.02", fid, str(yr), "HIGH", source_tag, evt.get("reasoning",""))
            _write_evidence(conn, bridge_id, "B.W.03", fid, "|".join(codes), "HIGH", source_tag, evt.get("reasoning",""))


def merge_vicinity_result(conn, bridge_id, result, page_info):
    source_tag = f"Dwg {page_info.get('drawing_number','')} ({page_info.get('doc_type_str','')} {page_info.get('year','')})"
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.W.01", source_tag)
    _merge_item(conn, bridge_id, "PRIMARY", result, "B.G.13", source_tag)

    if result.get("feature_name_carried", {}) and result["feature_name_carried"].get("value"):
        _write_evidence(conn, bridge_id, "B.F.03", "H01",
                        result["feature_name_carried"]["value"], "HIGH", source_tag, None)
    if result.get("feature_name_crossed", {}) and result["feature_name_crossed"].get("value"):
        # Determine feature ID from already-populated bridge record
        _write_evidence(conn, bridge_id, "B.F.03", "_CROSSED",
                        result["feature_name_crossed"]["value"], "HIGH", source_tag, None)

    for evt in result.get("work_events", []):
        yr = evt.get("year")
        codes = evt.get("work_codes", [])
        if yr and codes:
            fid = f"WORK:{yr}"
            _write_evidence(conn, bridge_id, "B.W.02", fid, str(yr), "HIGH", source_tag, evt.get("reasoning",""))
            _write_evidence(conn, bridge_id, "B.W.03", fid, "|".join(codes), "HIGH", source_tag, evt.get("reasoning",""))


def merge_clearance_result(conn, bridge_id, result, page_info):
    source_tag = f"Clearance diagram ({page_info.get('doc_type_str','')} {page_info.get('year','')})"

    for feat in result.get("features", []):
        fid = feat.get("feature_id_guess") or "H02"
        clr = feat.get("clearances", {})
        for item_id in ("B.H.12","B.H.13","B.H.14","B.H.15","B.RR.02","B.RR.03"):
            entry = clr.get(item_id, {})
            if isinstance(entry, dict) and entry.get("value") is not None:
                _write_evidence(conn, bridge_id, item_id, fid,
                                entry["value"],
                                entry.get("confidence","HIGH"),
                                source_tag,
                                entry.get("reasoning"))

    questions = result.get("questions", [])
    if questions:
        _append_question(conn, bridge_id, "B.H.12", "H01", " | ".join(questions))


# ── Dispatch ───────────────────────────────────────────────────────────────

MERGE_DISPATCH = {
    "PLAN":      merge_plan_result,
    "SECTION":   merge_section_result,
    "RAIL":      merge_rail_result,
    "NOTES":     merge_notes_result,
    "VICINITY":  merge_vicinity_result,
    "BENT":      lambda c, b, r, p: None,  # not merging bent details yet
    "CLEARANCE": merge_clearance_result,
}

def merge_result(conn, bridge_id, page_type, result, page_info):
    fn = MERGE_DISPATCH.get(page_type)
    if fn:
        fn(conn, bridge_id, result, page_info)


# ── Helpers ────────────────────────────────────────────────────────────────

_YEAR_ITEMS = {"B.W.01", "B.W.02"}

def _is_valid_year(val):
    """Return True only if val looks like a plausible 4-digit bridge construction year."""
    try:
        y = int(str(val).strip())
        return 1800 <= y <= 2100
    except (ValueError, TypeError):
        return False


def _merge_item(conn, bridge_id, feature_id, result, item_id, source_tag):
    entry = result.get(item_id)
    if not isinstance(entry, dict):
        return
    val = entry.get("value")
    if val is None:
        return
    # Reject non-year values for year items (guards against AI returning bridge IDs)
    if item_id in _YEAR_ITEMS and not _is_valid_year(val):
        return
    _write_evidence(conn, bridge_id, item_id, feature_id,
                    str(val), entry.get("confidence","HIGH"),
                    source_tag, entry.get("reasoning"))


def _write_evidence(conn, bridge_id, item_id, feature_id, plan_value,
                    confidence, source_pages, reasoning):
    item = ITEM_BY_ID.get(item_id, {})
    upsert_evidence(conn, {
        "bridge_id":        bridge_id,
        "item_id":          item_id,
        "feature_id":       feature_id,
        "item_name":        item.get("name",""),
        "plan_value":       str(plan_value),
        "plan_confidence":  confidence or "HIGH",
        "plan_source_pages":source_pages,
        "plan_reasoning":   reasoning,
        "status":           "PENDING",
    })


def _append_question(conn, bridge_id, item_id, feature_id, question):
    conn.execute("""
        UPDATE evidence SET
            auto_questions = CASE
                WHEN auto_questions IS NULL OR auto_questions = ''
                THEN ?
                ELSE auto_questions || ' | ' || ?
            END,
            updated_at = datetime('now')
        WHERE bridge_id=? AND item_id=? AND feature_id=?
    """, (question, question, bridge_id, item_id, feature_id))
