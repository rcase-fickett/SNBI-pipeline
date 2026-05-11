#!/usr/bin/env python3
"""
02_process_bridges.py — Phase 2: Extract SNBI data from plan PDFs via Claude API.

Processes each bridge's plan PDF and vertical clearance PDFs.
Can be stopped and restarted — already-processed bridges are skipped.
Progress is printed to console and logged to the database.

Usage:
    python 02_process_bridges.py              # process all pending bridges
    python 02_process_bridges.py --limit 10   # process only 10 (for testing)
    python 02_process_bridges.py --id 02283   # process one specific bridge
"""
import sys
import os
import argparse
import traceback
sys.path.insert(0, os.path.dirname(__file__))

import config
from lib.db import get_conn, get_bridges_by_status, get_bridge, set_bridge_status, log, print_stats, migrate_db, get_active_lessons
from lib.pdf_extractor import (
    load_metadata, build_page_plan, select_priority_pages,
    page_to_base64_jpeg, get_vc_pdf_paths, get_page_count
)
from lib.claude_api import ClaudeExtractorWithLessons as ClaudeExtractor
from lib.results_merger import merge_result
from lib.snbi_items import PAGE_PLAN, PAGE_SECTION, PAGE_RAIL, PAGE_NOTES, PAGE_VICINITY, PAGE_CLEARANCE
from lib.geo_context import build_context_block

# Which page types to request per bridge category
NEEDED_TYPES_BY_CATEGORY = {
    "WATERWAY": [PAGE_VICINITY, PAGE_PLAN, PAGE_SECTION, PAGE_RAIL, PAGE_NOTES],
    "HIGHWAY":  [PAGE_VICINITY, PAGE_PLAN, PAGE_SECTION, PAGE_RAIL, PAGE_NOTES, PAGE_CLEARANCE],
    "RAILROAD": [PAGE_VICINITY, PAGE_PLAN, PAGE_SECTION, PAGE_RAIL, PAGE_NOTES, PAGE_CLEARANCE],
    "UNKNOWN":  [PAGE_VICINITY, PAGE_PLAN, PAGE_SECTION, PAGE_RAIL, PAGE_NOTES],
}


def process_bridge(conn, bridge_id, extractor, verbose=True):
    """Process one bridge. Returns True on success."""
    bridge = get_bridge(conn, bridge_id)
    if not bridge:
        print(f"  [{bridge_id}] Not found in database — run Phase 1 first.")
        return False

    # Prefer dynamically-constructed path so the script works across machines
    plan_path = os.path.join(config.BRIDGES_ROOT, bridge_id, f"{bridge_id} Plans.pdf")
    if not os.path.exists(plan_path):
        plan_path = bridge["plan_pdf_path"]  # fall back to stored path
    if not plan_path or not os.path.exists(plan_path):
        msg = f"Plan PDF not found: {plan_path}"
        log(conn, bridge_id, "PLANS", "SKIPPED", msg)
        set_bridge_status(conn, bridge_id, "ERROR")
        if verbose:
            print(f"  [{bridge_id}] SKIP — {msg}")
        return False

    bridge_dir  = os.path.dirname(plan_path)
    category    = bridge["feature_category"] or "UNKNOWN"
    needed      = NEEDED_TYPES_BY_CATEGORY.get(category, NEEDED_TYPES_BY_CATEGORY["UNKNOWN"])

    # Look up nearby OSM features for prompt context (silent on failure)
    geo_ctx = build_context_block(bridge["lat"], bridge["lon"])
    if verbose and geo_ctx:
        print(f"  [{bridge_id}] OSM context loaded")

    # ── Load metadata and build page plan ─────────────────────
    metadata  = load_metadata(bridge_dir)
    page_plan = build_page_plan(plan_path, metadata)

    if not page_plan:
        msg = "Could not build page plan (PDF may be unreadable)"
        log(conn, bridge_id, "PLANS", "ERROR", msg)
        set_bridge_status(conn, bridge_id, "ERROR")
        if verbose:
            print(f"  [{bridge_id}] ERROR — {msg}")
        return False

    selected = select_priority_pages(page_plan, needed, max_pages=8)

    if verbose:
        print(f"  [{bridge_id}] {bridge['bridge_name'][:40]:<40} "
              f"| {category:<8} | {len(page_plan)} pages | {len(selected)} selected")

    construction_year = str(bridge["year_built"] or "").strip()

    def drawing_context(page_info):
        """Build a per-page context block telling Claude the drawing title and year."""
        dwg_year  = str(page_info.get("year", "")).strip()
        dwg_title = str(page_info.get("doc_type_str", "")).strip()
        dwg_num   = str(page_info.get("drawing_number", "")).strip()

        is_new_work = (dwg_year and construction_year and dwg_year != construction_year)
        is_standard = any(kw in dwg_title.upper() for kw in
                          ("STANDARD", "TYPICAL", "STD.", "GENERAL DETAIL",
                           "STAGING", "TEMPORARY", "FALSEWORK"))

        lines = [
            "\n\n--- DRAWING METADATA (use this to guide work event extraction) ---",
            f"Drawing title : {dwg_title}" + (f" (#{dwg_num})" if dwg_num else ""),
            f"Drawing year  : {dwg_year or 'unknown'}",
            f"Bridge original construction year: {construction_year or 'unknown'}",
        ]
        if is_standard:
            lines.append("Classification: STANDARD/TYPICAL DETAIL — do NOT extract work events from this sheet.")
        elif is_new_work:
            lines.append(f"Classification: POST-CONSTRUCTION work package (year {dwg_year} ≠ construction year {construction_year}).")
            lines.append(f"  → If this drawing shows structural work, report a work event for year {dwg_year}.")
            lines.append( "  → Consolidate: one work event per year — do not create a separate event per drawing.")
        else:
            lines.append("Classification: ORIGINAL CONSTRUCTION drawing — do not generate a work event.")
        lines.append("--- END DRAWING METADATA ---")
        return "\n".join(lines)

    # ── Process plan pages ─────────────────────────────────────
    pages_ok = 0
    for page_info in selected:
        try:
            img_b64 = page_to_base64_jpeg(
                plan_path,
                page_info["page_index"],
                dpi=config.IMAGE_DPI,
                max_px=config.MAX_IMAGE_PX,
            )
            ctx = geo_ctx + drawing_context(page_info)
            result, raw = extractor.extract_from_image(
                img_b64, page_info["page_type"], bridge_id, geo_context=ctx
            )
            if result:
                merge_result(conn, bridge_id, page_info["page_type"], result, page_info)
                pages_ok += 1
            else:
                log(conn, bridge_id, "PLANS", "ERROR",
                    f"Page {page_info['page_index']}: {raw[:200]}")
        except Exception as e:
            log(conn, bridge_id, "PLANS", "ERROR",
                f"Page {page_info['page_index']}: {traceback.format_exc()[-300:]}")

    # ── Process vertical clearance PDFs ───────────────────────
    vc_paths = get_vc_pdf_paths(bridge_dir, bridge_id)
    for vc_path in vc_paths:
        n_pages = get_page_count(vc_path)
        for pg in range(n_pages):
            try:
                img_b64 = page_to_base64_jpeg(vc_path, pg,
                                               dpi=config.IMAGE_DPI,
                                               max_px=config.MAX_IMAGE_PX)
                vc_info = {
                    "page_index":   pg,
                    "page_type":    PAGE_CLEARANCE,
                    "doc_type_str": "Clearance diagram",
                    "drawing_number":"",
                    "year":         "",
                }
                result, raw = extractor.extract_from_image(img_b64, PAGE_CLEARANCE, bridge_id)
                if result:
                    merge_result(conn, bridge_id, PAGE_CLEARANCE, result, vc_info)
                    pages_ok += 1
            except Exception as e:
                log(conn, bridge_id, "CLEARANCE", "ERROR", str(e)[-200:])

    # ── Mark items still PENDING ────────────────────────────────
    # Items with default_confidence=FIELD_REQ are always field-measured;
    # set those first, then sweep the rest to NOT_FOUND.
    from lib.snbi_items import ITEMS as _ITEMS
    field_req_ids = [i["id"] for i in _ITEMS
                     if i.get("default_confidence") == FIELD_REQ]
    if field_req_ids:
        placeholders = ",".join("?" * len(field_req_ids))
        conn.execute(f"""
            UPDATE evidence
            SET plan_confidence = 'FIELD_REQ', updated_at = datetime('now')
            WHERE bridge_id = ? AND plan_confidence = 'PENDING'
              AND item_id IN ({placeholders})
        """, [bridge_id] + field_req_ids)

    conn.execute("""
        UPDATE evidence
        SET plan_confidence = 'NOT_FOUND', updated_at = datetime('now')
        WHERE bridge_id = ? AND plan_confidence = 'PENDING'
    """, (bridge_id,))

    # ── Finalise ───────────────────────────────────────────────
    new_status = "PLANS_DONE" if pages_ok > 0 else "ERROR"
    set_bridge_status(conn, bridge_id, new_status)
    log(conn, bridge_id, "PLANS", "SUCCESS" if pages_ok > 0 else "ERROR",
        f"{pages_ok}/{len(selected)} pages extracted")
    conn.commit()
    return pages_ok > 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of bridges to process (for testing)")
    parser.add_argument("--id", type=str, default=None,
                        help="Process a single bridge ID")
    parser.add_argument("--reprocess", action="store_true",
                        help="Reprocess bridges even if already PLANS_DONE")
    args = parser.parse_args()

    print("=" * 60)
    print("  SNBI Pipeline — Phase 2: PDF Extraction via Claude API")
    print("=" * 60)

    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        print("  Windows: setx ANTHROPIC_API_KEY \"sk-ant-...\"  then restart terminal")
        sys.exit(1)

    conn = get_conn(config.DB_PATH)

    # Ensure feedback tables exist (safe on fresh DB)
    migrate_db(config.DB_PATH)

    # Load active lessons — these are injected into every extraction prompt
    lessons = get_active_lessons(conn)
    if lessons:
        print(f"  Active lessons loaded: {len(lessons)} items have inspector feedback")
        for iid in sorted(lessons.keys()):
            n = lessons[iid]["correction_count"]
            print(f"    {iid}: {n} correction{'s' if n!=1 else ''} incorporated")
    else:
        print("  No active lessons yet (run 05_build_lessons.py after first review cycle)")

    # Load reference documents (SNBI errata + datacrosswalk) for Claude context
    from lib.claude_api import load_reference_docs
    ref_docs = load_reference_docs(
        errata_pdf_path=getattr(config, "SNBI_ERRATA_PDF", None),
        crosswalk_path=getattr(config, "DATACROSSWALK_PATH", None),
    )

    extractor = ClaudeExtractor(
        api_key=config.ANTHROPIC_API_KEY,
        model=config.CLAUDE_MODEL,
        delay_sec=config.BATCH_DELAY_SEC,
        lessons=lessons,
        errata_pdf_path=ref_docs.get("errata_pdf_path"),
        crosswalk_text=ref_docs.get("crosswalk_text"),
    )

    # Get bridge list to process
    if args.id:
        bridges = [get_bridge(conn, args.id)]
        bridges = [b for b in bridges if b]
    elif args.reprocess:
        bridges = (get_bridges_by_status(conn, "BRM_DONE") +
                   get_bridges_by_status(conn, "PLANS_DONE") +
                   get_bridges_by_status(conn, "ERROR"))
    else:
        bridges = get_bridges_by_status(conn, "BRM_DONE")

    if config.BRIDGE_FILTER:
        bridges = [b for b in bridges if b["bridge_id"] in config.BRIDGE_FILTER]

    if args.limit:
        bridges = bridges[:args.limit]

    print(f"  Bridges to process: {len(bridges)}")
    if len(bridges) == 0:
        print("  Nothing to process. Run 01_init_db.py first or use --reprocess.")
        conn.close()
        return

    success = 0
    errors  = 0
    for i, bridge in enumerate(bridges, 1):
        bid = bridge["bridge_id"]
        print(f"\n[{i:>4}/{len(bridges)}] ", end="")
        try:
            ok = process_bridge(conn, bid, extractor, verbose=True)
            if ok:
                success += 1
            else:
                errors += 1
        except KeyboardInterrupt:
            print(f"\n\nInterrupted at bridge {i}. Progress saved to database.")
            break
        except Exception as e:
            print(f"  [{bid}] UNEXPECTED ERROR: {e}")
            log(conn, bid, "PLANS", "ERROR", str(e))
            set_bridge_status(conn, bid, "ERROR")
            conn.commit()
            errors += 1

    print(f"\n{'='*60}")
    print(f"  Done. Success: {success}  Errors: {errors}")
    try:
        print_stats(conn)
    except UnicodeEncodeError:
        pass  # box-drawing chars fail on cp1252 consoles; stats already in DB
    conn.close()
    print("Next step: run  python 03_export_csv.py")


if __name__ == "__main__":
    main()
