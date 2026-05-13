#!/usr/bin/env python3
"""
07_import_bridge_log.py — Import clearance pre-fills from ODOT Bridge Log PDF.

Reads brlog.pdf (text-extractable) and pre-fills:
  B.H.13  Highway Min Vertical Clearance        (VC/VCNB/VCSB/VCEB/VCWB)
  B.H.14  Highway Min Horizontal Clearance Left  (HC/HCNB/HCSB/HCEB/HCWB)
  B.H.15  Highway Min Horizontal Clearance Right (same source as B.H.14)

Clearance format: VCNB 16-09 = 16 ft 9 in NB -> 16.7 ft (floor to 0.1 ft).
All directional values (NB/SB/EB/WB) are aggregated to the minimum and written to
the below-highway feature(s) (B.F.02 = 'B', feature_id starts with 'H').
H01 is always the carried feature (99.9 pre-filled by Phase 1 — skip it).
HC writes to both B.H.14 and B.H.15.
If a bridge has multiple below-highway features (divided highway with something below),
an auto_question is added since the bridge log gives one undivided clearance value.

Confidence: APPROX. Only fills null brm_values — safe to re-run.
Phase 2 plan extraction will override with HIGH-confidence values where found.

Usage:
    python 07_import_bridge_log.py
    python 07_import_bridge_log.py --dry-run          # preview without writing
    python 07_import_bridge_log.py --sample           # dump first 3 pages of text and exit
    python 07_import_bridge_log.py --limit 20         # process first 20 matching bridges
"""

import os, sys, re, argparse, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH
from lib.db import get_conn, migrate_db, get_below_features

BRLOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brlog.pdf")
SOURCE_COL  = "ODOT Bridge Log 2024"

# Matches: VC 17-03 / VCNB 16-09 / HCNB 44-07 / HC 40-00 etc.
CLEARANCE_RE = re.compile(
    r'\b(HC|VC)(NB|SB|EB|WB)?\s+(\d{1,3})-(\d{2})\b',
    re.IGNORECASE,
)

# ODOT bridge IDs are exactly 5 zero-padded digits + optional letter suffix.
# Using \d{5} (not 4-5) avoids matching 4-digit years and catalog-bridge sub-IDs
# like the "8588" inside the catalog ID "R8588E".
BRIDGE_ID_RE = re.compile(r'\b(\d{5}[A-Z]?)\b')

# Below-highway features are resolved per bridge via get_below_features(conn, id, 'H').
# This handles divided highways where H02 may be a second carried feature, not the one below.


def to_decimal_ft(feet_str, inches_str):
    """Convert XX-YY (feet-inches) to decimal feet, rounded DOWN to nearest 0.1."""
    total = int(feet_str) + int(inches_str) / 12.0
    return math.floor(total * 10) / 10


def extract_pdf_pages(pdf_path):
    """Return list of (page_index, text) for every page in the PDF."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        print("ERROR: pypdfium2 not installed. Run: pip install pypdfium2")
        sys.exit(1)

    pages = []
    doc = pdfium.PdfDocument(pdf_path)
    for i in range(len(doc)):
        page = doc[i]
        tp   = page.get_textpage()
        text = tp.get_text_range()
        tp.close()
        page.close()
        pages.append((i, text))
    doc.close()
    return pages


def parse_clearances(pages, known_ids):
    """
    Scan pages for bridge IDs followed by clearance codes.

    Returns:
        { bridge_id: { 'VC': {direction_or_None: ft}, 'HC': {dir: ft}, 'lines': [str, ...] } }

    The bridge log has one bridge per entry line, formatted as:
        {milepost}  {bridge_id}  {name ...}  {HC/VC codes}
    Continuation lines (no milepost) carry overflow clearance codes for the same bridge.

    Strategy:
    - ENTRY_LINE_RE detects new bridge entry lines (decimal milepost + 4-5 digit ID).
    - On entry lines: update current_id (None if bridge not in our DB, so sign bridges
      and other non-SNBI structures don't absorb clearances from the previous bridge).
    - On continuation lines: keep current_id and collect any clearance codes.
    - When multiple values exist for the same (code, direction) pair, keep the minimum.
    """
    # Detect new entry lines by milepost pattern at the line start.
    # Bridge log entry lines always begin with an optional prefix (letters/digits/dash)
    # followed by a decimal milepost (e.g. "21.92", "6C301.77", "X-2.71", "2C21.21").
    # Using a broad pattern here ensures that catalog-bridge entries like "7C301.81 R8588E"
    # are detected as new entry lines (not continuation of the previous SNBI bridge),
    # even though their bridge IDs don't match our \d{5} pattern.
    ENTRY_LINE_RE = re.compile(r'^[A-Z0-9*-]*\d+\.\d+\s')

    results    = {}
    current_id = None

    for _page_idx, text in pages:
        for line in text.splitlines():
            is_entry = bool(ENTRY_LINE_RE.search(line))

            if is_entry:
                # New bridge entry — determine which bridge it belongs to
                new_id = None
                for m in BRIDGE_ID_RE.finditer(line):
                    candidate = m.group(1)
                    if candidate in known_ids:
                        new_id = candidate
                        break
                # Reset tracking: None for unknown bridges (sign bridges, culverts, etc.)
                current_id = new_id
                if current_id and current_id not in results:
                    results[current_id] = {'VC': {}, 'HC': {}, 'lines': []}

            # Collect clearance codes and raw lines (entry or continuation)
            if current_id:
                stripped = line.strip()
                if stripped:
                    results[current_id]['lines'].append(stripped)
                for m in CLEARANCE_RE.finditer(line):
                    code      = m.group(1).upper()
                    direction = m.group(2).upper() if m.group(2) else None
                    ft_val    = to_decimal_ft(m.group(3), m.group(4))
                    prior = results[current_id][code].get(direction)
                    if prior is None or ft_val < prior:
                        results[current_id][code][direction] = ft_val

    return results


def upsert_feature_ev(conn, bridge_id, feature_id, item_id, item_name, value, dry_run,
                      auto_q=None):
    """
    Insert or update a feature evidence row — fills gaps only (never overwrites).
    Returns 'inserted', 'updated', or None.
    """
    row = conn.execute(
        "SELECT id, brm_value FROM evidence WHERE bridge_id=? AND item_id=? AND feature_id=?",
        (bridge_id, item_id, feature_id),
    ).fetchone()

    if row:
        if row["brm_value"]:
            return None  # already populated — leave it alone
        if not dry_run:
            conn.execute(
                "UPDATE evidence SET brm_value=?, brm_source_col=?, auto_questions=?, "
                "updated_at=datetime('now') WHERE id=?",
                (str(value), SOURCE_COL, auto_q, row["id"]),
            )
        return "updated"
    else:
        if not dry_run:
            conn.execute(
                "INSERT INTO evidence "
                "(bridge_id, item_id, feature_id, item_name, brm_value, "
                " brm_source_col, auto_questions, plan_confidence, status) "
                "VALUES (?,?,?,?,?,?,?,'PENDING','PENDING')",
                (bridge_id, item_id, feature_id, item_name, str(value), SOURCE_COL, auto_q),
            )
        return "inserted"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing to DB")
    parser.add_argument("--sample",  action="store_true",
                        help="Dump extracted text from first 3 pages and exit")
    parser.add_argument("--limit",   type=int, default=None,
                        help="Process only first N matching bridges (for testing)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Phase 7: Import ODOT Bridge Log Clearances")
    print("=" * 60)

    if not os.path.exists(BRLOG_PATH):
        print(f"ERROR: {BRLOG_PATH} not found.")
        print("  Place brlog.pdf in the project root and try again.")
        sys.exit(1)

    print(f"  PDF : {BRLOG_PATH}")
    print("  Extracting text from PDF (this may take a moment)...")
    pages = extract_pdf_pages(BRLOG_PATH)
    print(f"  Pages: {len(pages)}")

    if args.sample:
        for page_idx, text in pages[:3]:
            print(f"\n{'='*40} PAGE {page_idx} {'='*40}")
            print(text[:3000] if text else "(empty)")
        return

    conn = get_conn(DB_PATH)
    migrate_db(DB_PATH)

    our_ids = set(r[0] for r in conn.execute("SELECT bridge_id FROM bridges").fetchall())
    print(f"  Bridges in DB : {len(our_ids)}")

    print("  Scanning for bridge IDs and clearance codes...")
    clearances = parse_clearances(pages, our_ids)
    found_ids  = sorted(clearances.keys())
    print(f"  Bridges found in log: {len(found_ids)}")

    if args.limit:
        found_ids = found_ids[:args.limit]
        print(f"  (limited to {args.limit})")

    if args.dry_run:
        print("  DRY RUN — no changes will be written\n")

    n_inserted = 0
    n_updated  = 0
    n_skipped  = 0

    for bridge_id in found_ids:
        data = clearances[bridge_id]

        # Find the actual below-highway features for this bridge.
        # Normally one (H02), but after discover_features() may be H03 or higher
        # for divided-highway bridges where H02 is a second carried feature.
        h_below = get_below_features(conn, bridge_id, 'H')
        if not h_below:
            n_skipped += 1
            continue

        multi = len(h_below) > 1
        ambiguous_aq = ("Bridge log gives a single clearance value but multiple below-highway "
                        "features exist. Verify which feature this applies to.") if multi else None

        # Aggregate across all directions — take the minimum (most restrictive) value.
        for code, dir_map in data.items():
            if code == 'lines':
                continue
            if not dir_map:
                continue
            min_ft = min(dir_map.values())
            dirs   = ", ".join(d or "undirected" for d in dir_map)

            # VC → B.H.13 only; HC → B.H.14 and B.H.15
            if code == 'VC':
                write_items = [("B.H.13", "Highway Min Vertical Clearance")]
            else:
                write_items = [
                    ("B.H.14", "Highway Min Horizontal Clearance Left"),
                    ("B.H.15", "Highway Min Horizontal Clearance Right"),
                ]

            for below_fid in h_below:
                for item_id, item_name in write_items:
                    outcome = upsert_feature_ev(
                        conn, bridge_id, below_fid,
                        item_id, item_name, min_ft, args.dry_run,
                        auto_q=ambiguous_aq,
                    )
                    if outcome == "inserted":
                        n_inserted += 1
                        if args.dry_run:
                            print(f"  INSERT [{bridge_id}] {below_fid} {item_id} = {min_ft} ft  ({dirs})")
                    elif outcome == "updated":
                        n_updated += 1
                        if args.dry_run:
                            print(f"  UPDATE [{bridge_id}] {below_fid} {item_id} = {min_ft} ft  ({dirs})")
                    else:
                        n_skipped += 1

    # Write raw entry text to bridge_log table
    log_written = 0
    for bridge_id in found_ids:
        raw_lines = clearances[bridge_id].get('lines', [])
        if not raw_lines:
            continue
        raw_entry = "\n".join(raw_lines)
        if not args.dry_run:
            conn.execute(
                "INSERT INTO bridge_log (bridge_id, raw_entry) VALUES (?,?) "
                "ON CONFLICT(bridge_id) DO UPDATE SET raw_entry=excluded.raw_entry, "
                "imported_at=datetime('now')",
                (bridge_id, raw_entry),
            )
        log_written += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n  Bridges with clearance data : {len(found_ids)}")
    print(f"  Bridge log entries stored   : {log_written}")
    print(f"  Evidence rows inserted      : {n_inserted}")
    print(f"  Evidence rows updated (null): {n_updated}")
    print(f"  Evidence rows skipped (full): {n_skipped}")

    if args.dry_run:
        print("\n  Dry run complete — run without --dry-run to apply changes.")
    else:
        print("\n  Import complete.")
        print("  These are APPROX pre-fills. Phase 2 plan extraction provides HIGH-confidence overrides.")


if __name__ == "__main__":
    main()
