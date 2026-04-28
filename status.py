#!/usr/bin/env python3
"""
status.py — Show pipeline status at a glance.

Usage:
    python status.py              # full summary
    python status.py --lessons    # show active lesson text
    python status.py --errors     # show bridges with errors
    python status.py --flagged    # show inspector-flagged items
"""
import sys
import os
import argparse
sys.path.insert(0, os.path.dirname(__file__))

import config
from lib.db import get_conn, print_stats, get_lesson_stats, get_active_lessons


def show_errors(conn):
    rows = conn.execute("""
        SELECT b.bridge_id, b.bridge_name, p.message, p.created_at
        FROM bridges b
        JOIN processing_log p ON b.bridge_id = p.bridge_id
        WHERE b.processing_status = 'ERROR' AND p.status = 'ERROR'
        ORDER BY p.created_at DESC
    """).fetchall()
    print(f"\n── Bridges with errors ({len(rows)}) ──────────────────────")
    for r in rows:
        print(f"  {r['bridge_id']:10s} {r['bridge_name'][:35]:<35} {r['message'][:60]}")


def show_flagged(conn):
    rows = conn.execute("""
        SELECT e.bridge_id, e.item_id, e.feature_id,
               e.plan_value, e.user_determination, e.user_notes,
               e.reviewed_by
        FROM evidence e
        WHERE e.status = 'FLAGGED'
        ORDER BY e.bridge_id, e.item_id
    """).fetchall()
    print(f"\n── Inspector-flagged items ({len(rows)}) ────────────────────")
    for r in rows:
        print(f"  {r['bridge_id']:8s} {r['item_id']:8s} {r['feature_id']:10s} "
              f"AI={r['plan_value']!r:15s} FIELD={r['user_determination']!r:15s}")
        if r['user_notes']:
            print(f"           Note: {r['user_notes'][:80]}")


def show_cycle_summary(conn):
    """Show how many bridges are at each stage of the feedback cycle."""
    print("\n── Feedback cycle summary ──────────────────────────────")

    # Corrections by item
    rows = conn.execute("""
        SELECT item_id,
            COUNT(*) as total,
            SUM(CASE WHEN correction_type != 'CONFIRMED' THEN 1 ELSE 0 END) as wrong,
            SUM(CASE WHEN correction_type  = 'CONFIRMED' THEN 1 ELSE 0 END) as right,
            SUM(CASE WHEN used_in_lesson=0 THEN 1 ELSE 0 END) as new_unused
        FROM corrections
        GROUP BY item_id
        ORDER BY wrong DESC
    """).fetchall()

    if not rows:
        print("  No corrections imported yet.")
        print("  After first inspector review: run 04_import_corrections.py")
        return

    print(f"  {'Item':12s} {'Wrong':>6} {'Right':>6} {'New':>6}  {'Accuracy':>8}")
    for r in rows:
        total = r['total']
        acc = r['right'] / total * 100 if total else 0
        print(f"  {r['item_id']:12s} {r['wrong']:>6} {r['right']:>6} "
              f"{r['new_unused']:>6}  {acc:>7.0f}%")

    # Batch history
    batches = conn.execute(
        "SELECT * FROM import_batches ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    if batches:
        print(f"\n  Recent import batches:")
        for b in batches:
            print(f"    {b['batch_id']:20s} corrections={b['corrections']:3d} "
                  f"confirmations={b['confirmations']:3d}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lessons",  action="store_true")
    parser.add_argument("--errors",   action="store_true")
    parser.add_argument("--flagged",  action="store_true")
    args = parser.parse_args()

    if not os.path.exists(config.DB_PATH):
        print(f"Database not found: {config.DB_PATH}")
        print("Run 01_init_db.py first.")
        sys.exit(1)

    conn = get_conn(config.DB_PATH)

    print("=" * 60)
    print("  SNBI Pipeline Status")
    print("=" * 60)

    print_stats(conn)

    if args.errors:
        show_errors(conn)
    elif args.flagged:
        show_flagged(conn)
    elif args.lessons:
        lessons = get_active_lessons(conn)
        if not lessons:
            print("No active lessons yet.")
        for item_id, lesson in sorted(lessons.items()):
            print(f"\n── {item_id} (v{lesson['version']}, "
                  f"{lesson['correction_count']} corrections, "
                  f"confidence={lesson['confidence_score']:.2f}) ──")
            print(f"  {lesson['lesson_text']}")
    else:
        get_lesson_stats(conn)
        show_cycle_summary(conn)
        print("\nOptions: --lessons  --errors  --flagged")

    conn.close()


if __name__ == "__main__":
    main()
