# dpl/lib/manifest_compare.py
"""Equivalence comparator for extraction manifest folders.

Purpose: the Phase 4 equivalence drill. Two runs on the same day, same
machine, same data - old code vs new code at knobs 1/1 - must produce
the same extraction results. This module compares them mechanically so
an operator never eyeballs JSON.

Field treatment, and WHY each is treated that way:

  IGNORED (can never match between two runs):
      envelope: run_id, started_at, finished_at
      table   : duration_sec, attempts (attempts is new in Phase 4;
                old-code baselines lack it, so it is ignored, not
                compared)
      wrapper : run_finished_at

  COMPARED (the substance of equivalence):
      envelope: profile, status, load_date, pending_tables,
                wrapper.exit_code, and the Part B summary counts
                (tables_total/extracted/skipped/failed) when present
                in BOTH sides
      table   : connection, schema, name, status, error presence,
                output_path (after separator normalization),
                rows_read, rows_written

  SEVERITY:
      status / table-set / pending / exit-code / summary mismatches
      are FATAL (exit 1).
      Row-count deltas, differing error texts, and path differences
      are NOTES (printed, verdict still MATCH): same-day intraday
      data drift is legitimate - an account created mid-morning may
      legitimately change a row count - and must not fail the drill.
      The operator reads the notes and judges.

Directory layout accepted (either shape):
      <dir>/<folder>/_manifest_<run_id>.json     (normal runs date dir)
      <dir>/_manifest_<run_id>.json              (flat copies)
When several manifest files sit in one folder, the newest by filename
is compared - run_id embeds the timestamp, so filename order is
chronological order.
"""

from __future__ import annotations

import json
from pathlib import Path

# envelope keys that can never match between two runs
ENVELOPE_VOLATILE = ("run_id", "started_at", "finished_at")
# table keys that can never match (or are new and absent in baselines)
TABLE_VOLATILE = ("duration_sec", "attempts")
# Part B summary counts - compared only when both sides have them
SUMMARY_KEYS = ("tables_total", "tables_extracted", "tables_skipped",
                "tables_failed")


def _newest(folder: Path) -> Path | None:
    """Latest _manifest_*.json in a folder (run_id sort = time sort)."""
    files = sorted(folder.glob("_manifest_*.json"))
    return files[-1] if files else None


def collect_manifests(root: str | Path) -> dict[str, dict]:
    """Read a manifest directory -> {folder_name: envelope}.

    Accepts the normal layout (one subdirectory per folder) or a flat
    directory of manifest files (folder key taken from the envelope).
    Raises FileNotFoundError when the directory or manifests are absent.
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"manifest directory not found: {root}")

    out: dict[str, dict] = {}

    flat = _newest(root)
    if flat is not None:
        env = json.loads(flat.read_text())
        out[str(env.get("folder", root.name))] = env
        return out

    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        m = _newest(sub)
        if m is None:
            continue
        out[sub.name] = json.loads(m.read_text())

    if not out:
        raise FileNotFoundError(f"no _manifest_*.json found under {root}")
    return out


def _norm_path(value: object) -> str | None:
    """Normalize path separators (Windows vs POSIX) for comparison."""
    if value is None:
        return None
    return str(value).replace("\\", "/")


def compare(baseline: dict[str, dict],
            candidate: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Diff two {folder: envelope} mappings.

    Returns (fatal, notes). fatal empty <=> manifests equivalent.
    """
    fatal: list[str] = []
    notes: list[str] = []

    b_folders, c_folders = set(baseline), set(candidate)
    for f in sorted(b_folders - c_folders):
        fatal.append(f"folder '{f}' in baseline but MISSING in candidate")
    for f in sorted(c_folders - b_folders):
        fatal.append(f"folder '{f}' in candidate but ABSENT in baseline")

    for folder in sorted(b_folders & c_folders):
        b, c = baseline[folder], candidate[folder]

        # envelope substance
        for key in ("profile", "status", "load_date"):
            if b.get(key) != c.get(key):
                fatal.append(f"[{folder}] envelope '{key}': "
                             f"baseline={b.get(key)!r} "
                             f"candidate={c.get(key)!r}")
        bpend = sorted(b.get("pending_tables") or [])
        cpend = sorted(c.get("pending_tables") or [])
        if bpend != cpend:
            fatal.append(f"[{folder}] pending_tables differ: "
                         f"baseline={bpend} candidate={cpend}")
        bwrap = b.get("wrapper") or {}
        cwrap = c.get("wrapper") or {}
        if bwrap.get("exit_code") != cwrap.get("exit_code"):
            fatal.append(f"[{folder}] wrapper exit_code: "
                         f"baseline={bwrap.get('exit_code')!r} "
                         f"candidate={cwrap.get('exit_code')!r}")
        for k in SUMMARY_KEYS:
            if k in b and k in c and b[k] != c[k]:
                fatal.append(f"[{folder}] summary '{k}': "
                             f"baseline={b[k]!r} candidate={c[k]!r}")

        # table-level substance
        bt = {t.get("name"): t for t in b.get("tables") or []}
        ct = {t.get("name"): t for t in c.get("tables") or []}
        for name in sorted(set(bt) - set(ct)):
            fatal.append(f"[{folder}] table '{name}' in baseline but "
                         "MISSING in candidate")
        for name in sorted(set(ct) - set(bt)):
            fatal.append(f"[{folder}] table '{name}' in candidate but "
                         "ABSENT in baseline")

        for name in sorted(set(bt) & set(ct)):
            tb, tc = bt[name], ct[name]
            for key in ("connection", "schema", "status"):
                if tb.get(key) != tc.get(key):
                    fatal.append(f"[{folder}] table '{name}': '{key}' "
                                 f"differs: {tb.get(key)!r} vs "
                                 f"{tc.get(key)!r}")
            berr, cerr = tb.get("error"), tc.get("error")
            if (berr is None) != (cerr is None):
                fatal.append(f"[{folder}] table '{name}': error presence "
                             f"differs: {berr!r} vs {cerr!r}")
            elif berr is not None and berr != cerr:
                notes.append(f"[{folder}] table '{name}': error texts "
                             "differ (both non-null)")
            for key in ("rows_read", "rows_written"):
                bv, cv = tb.get(key), tc.get(key)
                if bv != cv:
                    delta = ""
                    if isinstance(bv, int) and isinstance(cv, int):
                        delta = f" (delta {cv - bv:+d})"
                    notes.append(f"[{folder}] table '{name}': {key} "
                                 f"baseline={bv!r} candidate={cv!r}{delta}")
            bp = _norm_path(tb.get("output_path"))
            cp = _norm_path(tc.get("output_path"))
            if bp != cp:
                notes.append(f"[{folder}] table '{name}': output_path "
                             f"differs after normalization: {bp!r} vs {cp!r}")

    return fatal, notes