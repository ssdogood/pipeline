#!/usr/bin/env python
"""Compare two extraction-manifest folders for equivalence.

Usage (from the pipeline root):
    python scripts/compare_runs.py <baseline_dir> <candidate_dir>

    baseline_dir   manifest folder captured BEFORE the change
    candidate_dir  manifest folder produced by the NEW code, same day

Exit codes:
    0  equivalent (notes may exist - read them)
    1  fatal differences found
    2  usage or file error
"""
from __future__ import annotations

import json
import sys

from dpl.lib.manifest_compare import collect_manifests, compare


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__)
        return 2

    try:
        baseline = collect_manifests(argv[0])
        candidate = collect_manifests(argv[1])
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2

    print(f"baseline : {argv[0]}  ({len(baseline)} folder manifest(s))")
    print(f"candidate: {argv[1]}  ({len(candidate)} folder manifest(s))")
    print()

    fatal, notes = compare(baseline, candidate)
    for line in notes:
        print(f"note: {line}")
    for line in fatal:
        print(f"FATAL: {line}")
    print()

    if fatal:
        print(f"VERDICT: DIFFERENCES FOUND "
              f"({len(fatal)} fatal, {len(notes)} note(s))")
        return 1
    print(f"VERDICT: MATCH - manifests equivalent "
          f"({len(notes)} note(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())