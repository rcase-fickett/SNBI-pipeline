#!/usr/bin/env python3
"""
01_init_db.py — Phase 1: Initialize database and load all BrM data.

Run this first. No API calls are made — this only reads the Excel files
and your local folder structure. Takes about 30-60 seconds for 600 bridges.

Usage:
    python 01_init_db.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import config
from lib.db import init_db, get_conn, print_stats
from lib.brm_loader import load_brm_data


def main():
    print("=" * 60)
    print("  SNBI Pipeline — Phase 1: Database Init + BrM Load")
    print("=" * 60)

    # Validate API key exists (needed later, good to check now)
    if not config.ANTHROPIC_API_KEY:
        print("\nWARNING: ANTHROPIC_API_KEY not set.")
        print("  Set it before running Phase 2:")
        print("  Windows: setx ANTHROPIC_API_KEY \"sk-ant-...\"")
        print("  (Phase 1 does not need it — continuing)\n")

    # Validate input files exist
    for label, path in [
        ("BrM export",     config.BRM_EXPORT_PATH),
        ("Bridge list",    config.BRIDGE_LIST_PATH),
        ("VC list",        config.VC_LIST_PATH),
    ]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found: {path}")
            sys.exit(1)

    # Check bridges root
    if not os.path.exists(config.BRIDGES_ROOT):
        print(f"WARNING: Bridges root folder not found: {config.BRIDGES_ROOT}")
        print("  Plan PDF paths will be empty — update config.py if needed.\n")

    # Initialise DB
    init_db(config.DB_PATH)

    # Load BrM data
    conn = get_conn(config.DB_PATH)
    try:
        load_brm_data(conn, config)
    finally:
        conn.close()

    # Print stats
    conn = get_conn(config.DB_PATH)
    print_stats(conn)
    conn.close()

    print("Phase 1 complete.")
    print(f"Database: {config.DB_PATH}")
    print("Next step: run  python 02_process_bridges.py")


if __name__ == "__main__":
    main()
