#!/usr/bin/env python3
"""
05_build_lessons.py — Phase 5: Distil inspector corrections into reusable lessons.

For each SNBI item that has new corrections since the last lesson build,
asks Claude to synthesize a concise, actionable lesson that will be injected
into future extraction prompts. The lesson describes:
  - What the AI commonly gets wrong for this item
  - The correct interpretation rule
  - A concrete before/after example

Lessons are versioned — each run creates a new version superseding the old one,
so the AI gets progressively better as corrections accumulate.

Usage:
    python 05_build_lessons.py              # build lessons for all items with new corrections
    python 05_build_lessons.py --item B.G.01  # rebuild one specific item
    python 05_build_lessons.py --min-corrections 3  # only build if ≥3 new corrections
"""
import sys
import os
import json
import argparse
import time
sys.path.insert(0, os.path.dirname(__file__))

import config
import anthropic
from lib.db import (
    get_conn, migrate_db, get_corrections_for_item, get_all_corrections,
    mark_corrections_used, upsert_lesson, get_lesson_stats, get_active_lessons
)
from lib.snbi_items import ITEM_BY_ID, ITEMS


LESSON_SYSTEM_PROMPT = """You are an expert bridge engineer and AI trainer.
You are reviewing cases where an AI extraction system made errors (or correct calls)
when reading SNBI data items from bridge engineering plan drawings.

Your job is to synthesize these cases into a CONCISE, ACTIONABLE lesson that will
be prepended to future AI extraction prompts to prevent the same mistakes.

Rules for writing lessons:
- Be specific about what to look for and what to avoid
- Focus on measurement methodology errors, not general bridge knowledge
- Include the key distinction that separates correct from incorrect interpretation
- Maximum 4 sentences for lesson_text
- The example must be a real-looking JSON snippet showing correct extraction
- If corrections conflict with each other, note the ambiguity and give the most common answer

Return ONLY valid JSON with this structure:
{
  "lesson_text": "Concise lesson text here (max 4 sentences)",
  "example_json": {
    "value": "22.9",
    "confidence": "FIELD_REQ",
    "reasoning": "Measured between faces of pile bent caps (not o/o slab). Slab overhangs ~1ft each end.",
    "source": "Plan & Elevation, Dwg 37397"
  },
  "confidence_score": 0.85,
  "notes": "Any caveats or conflicting signals in the data"
}
"""


def build_lesson_prompt(item_id, item_def, corrections, confirmations):
    """Build the user message for lesson synthesis."""
    item_name = item_def.get("name", item_id)
    item_notes = item_def.get("notes", "")

    lines = [
        f"SNBI Item: {item_id} — {item_name}",
        f"Current spec guidance: {item_notes}",
        "",
        f"CORRECTIONS ({len(corrections)} cases where AI was wrong):",
    ]

    for c in corrections[:15]:  # cap at 15 to stay within token budget
        lines.append(
            f"  Bridge {c['bridge_id']}: AI said {c['ai_value']!r} "
            f"({c['ai_confidence']}) → Inspector said {c['field_value']!r}"
        )
        if c['field_notes']:
            lines.append(f"    Inspector note: {c['field_notes']}")
        if c['ai_reasoning']:
            lines.append(f"    AI reasoning was: {c['ai_reasoning']}")

    if confirmations:
        lines += [
            "",
            f"CONFIRMATIONS ({len(confirmations)} cases where AI was correct):",
        ]
        for c in confirmations[:5]:
            lines.append(
                f"  Bridge {c['bridge_id']}: AI said {c['ai_value']!r} — CONFIRMED correct"
            )
            if c['ai_reasoning']:
                lines.append(f"    AI reasoning: {c['ai_reasoning']}")

    lines += [
        "",
        "Based on these cases, write a lesson that will help the AI avoid these errors.",
        "Focus on the measurement methodology, not general engineering knowledge.",
    ]

    return "\n".join(lines)


def call_claude_for_lesson(client, model, item_id, item_def, corrections, confirmations):
    """Ask Claude to synthesize a lesson. Returns (lesson_text, example_json, confidence_score)."""
    prompt = build_lesson_prompt(item_id, item_def, corrections, confirmations)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=800,
            system=LESSON_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
        return (
            result.get("lesson_text", ""),
            json.dumps(result.get("example_json", {})),
            float(result.get("confidence_score", 0.5)),
        )
    except Exception as e:
        return None, None, 0.0


def process_item(conn, client, model, item_id, min_corrections=1, force=False):
    """Build or refresh lesson for one item. Returns True if lesson was written."""
    item_def = ITEM_BY_ID.get(item_id, {})

    # Get corrections for this item
    all_corr = get_corrections_for_item(conn, item_id, unused_only=not force)
    corrections    = [c for c in all_corr if c["correction_type"] != "CONFIRMED"]
    confirmations  = [c for c in all_corr if c["correction_type"] == "CONFIRMED"]

    if len(corrections) < min_corrections:
        print(f"  {item_id:12s} — {len(corrections)} corrections (need {min_corrections}) — SKIP")
        return False

    print(f"  {item_id:12s} — {len(corrections)} corrections, {len(confirmations)} confirmations — BUILDING...")

    lesson_text, example_json, confidence = call_claude_for_lesson(
        client, model, item_id, item_def, corrections, confirmations
    )

    if not lesson_text:
        print(f"    ERROR: Claude returned no lesson for {item_id}")
        return False

    # Write lesson to DB
    upsert_lesson(
        conn,
        item_id       = item_id,
        lesson_text   = lesson_text,
        example_json  = example_json,
        correction_count  = len(corrections),
        confirmed_count   = len(confirmations),
        confidence_score  = confidence,
    )

    # Mark corrections as used
    mark_corrections_used(conn, [c["id"] for c in all_corr])

    print(f"    Lesson (v{confidence:.2f}): {lesson_text[:120]}...")
    time.sleep(0.5)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", default=None,
                        help="Process a single item ID (e.g. B.G.01)")
    parser.add_argument("--min-corrections", type=int, default=2,
                        help="Minimum new corrections before building a lesson (default: 2)")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild lessons even using already-used corrections")
    parser.add_argument("--show-lessons", action="store_true",
                        help="Print current active lessons and exit")
    args = parser.parse_args()

    print("=" * 60)
    print("  SNBI Pipeline — Phase 5: Build Lessons from Corrections")
    print("=" * 60)

    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    if not os.path.exists(config.DB_PATH):
        print(f"ERROR: Database not found: {config.DB_PATH}")
        sys.exit(1)

    conn = get_conn(config.DB_PATH)
    migrate_db(config.DB_PATH)

    # ── Show current lessons and exit ──────────────────────────
    if args.show_lessons:
        lessons = get_active_lessons(conn)
        if not lessons:
            print("  No active lessons yet.")
        for item_id, lesson in sorted(lessons.items()):
            print(f"\n  {item_id} (v{lesson['version']}, "
                  f"based on {lesson['correction_count']} corrections, "
                  f"confidence={lesson['confidence_score']:.2f}):")
            print(f"    {lesson['lesson_text']}")
        conn.close()
        return

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # ── Determine which items to process ───────────────────────
    if args.item:
        items_to_process = [args.item]
    else:
        # All items that have unused corrections
        all_corr = get_all_corrections(conn)
        items_with_corrections = set(
            c["item_id"] for c in all_corr
            if not c["used_in_lesson"] or args.force
        )
        items_to_process = sorted(items_with_corrections)

    print(f"  Items to process: {len(items_to_process)}")
    get_lesson_stats(conn)

    built = 0
    skipped = 0
    for item_id in items_to_process:
        ok = process_item(
            conn, client, config.CLAUDE_MODEL,
            item_id,
            min_corrections=args.min_corrections,
            force=args.force,
        )
        if ok:
            built += 1
            conn.commit()
        else:
            skipped += 1

    conn.commit()
    print(f"\n  Done. Lessons built: {built}  Skipped: {skipped}")
    get_lesson_stats(conn)

    if built > 0:
        print("Lessons are now active. Next time you run 02_process_bridges.py,")
        print("they will be injected into extraction prompts automatically.")

    conn.close()


if __name__ == "__main__":
    main()
