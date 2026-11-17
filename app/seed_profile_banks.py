#!/usr/bin/env python3
"""Seed each education profile with a bank of interactive tests using the local AI.

Usage:
    python seed_profile_banks.py            # ensure 20 tests for ALL profiles
    python seed_profile_banks.py --profile mathematics --count 20

Requires a running local LM Studio (or compatible) endpoint configured in server.py.
For each profile it calls education_module.ensure_profile_bank, which only
generates the missing tests (so re-running is safe/idempotent).
"""
import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import modules.education_module as EDU


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None, help="Only seed this profile id")
    ap.add_argument("--count", type=int, default=20, help="Target tests per profile")
    args = ap.parse_args()

    profiles = EDU.list_profiles()
    if args.profile:
        profiles = [p for p in profiles if p["id"] == args.profile]
    if not profiles:
        print("No profiles found.")
        return

    print(f"Seeding {len(profiles)} profile(s) with up to {args.count} tests each...\n")
    for p in profiles:
        pid = p["id"]
        t0 = time.time()
        try:
            res = EDU.ensure_profile_bank(pid, args.count)
            print(f"  [{pid}] total={res.get('total')} added={res.get('added')} "
                  f"({time.time()-t0:.1f}s)")
        except Exception as e:
            print(f"  [{pid}] ERROR: {e} ({time.time()-t0:.1f}s)")

    print("\nDone. Banks stored in data/education/profiles/<id>_tests.json")


if __name__ == "__main__":
    main()
