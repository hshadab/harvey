"""Summarise every scored run so parity is judged on the spread.

Reads results/*/scores.json from the LAB checkout and reports criteria
passed per run, grouped by configuration, with the range. Also checks each
deliverable against the privilege index, so the security property and the
quality number are read side by side — the runB bypass (2026-08-14) looked
clean in the ledger and only showed up in the output.

Usage: python3 -m scripts.summarize_runs
"""

import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hook.privileged import PrivilegedIndex  # noqa: E402

LAB = Path(os.environ.get("LAB_DIR", Path.home() / "harvey-labs"))
TASK = "corporate-ma/review-data-room-red-flag-review"
DOCS = LAB / "tasks" / TASK / "documents"


def deliverable_text(run_dir: Path) -> str:
    for name in ("red-flag-memo.docx", "red-flag-memo.md"):
        f = run_dir / "output" / name
        if f.exists():
            if f.suffix == ".md":
                return f.read_text(errors="replace")
            try:
                return subprocess.run(["pandoc", "-t", "plain", str(f)],
                                      capture_output=True, text=True,
                                      timeout=120).stdout
            except (OSError, subprocess.SubprocessError):
                return ""
    return ""


def main():
    idx = PrivilegedIndex(DOCS) if DOCS.exists() else None
    rows = []
    for scores in sorted(LAB.glob("results/*/scores.json")):
        d = json.loads(scores.read_text())
        run_dir = scores.parent
        text = deliverable_text(run_dir)
        leak = idx.reproduced_from(text) if (idx and text) else []
        rows.append({
            "run": run_dir.name,
            "passed": d.get("n_passed"),
            "of": d.get("n_criteria"),
            "chars": len(text),
            "privileged": ",".join(leak) if leak else "clean",
        })

    if not rows:
        print("no scored runs found under", LAB / "results")
        return

    w = max(len(r["run"]) for r in rows)
    print(f"{'run':{w}}  {'criteria':>9}  {'chars':>7}  privileged")
    for r in rows:
        print(f"{r['run']:{w}}  {r['passed']:>4}/{r['of']:<4}  "
              f"{r['chars']:>7}  {r['privileged']}")

    print()
    for label, pat in (("unguarded (A)", r"^runA"), ("guarded (B)", r"^runB")):
        vals = [r["passed"] for r in rows
                if re.match(pat, r["run"]) and r["passed"] is not None]
        if not vals:
            continue
        spread = f"{min(vals)}-{max(vals)}"
        mean = statistics.mean(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        print(f"{label:14} n={len(vals)}  range {spread:>7}  "
              f"mean {mean:5.1f}  sd {sd:4.1f}")
    print("\nParity is the overlap of these ranges, not a single pair "
          "(SPEC.md 2.1.3).")


if __name__ == "__main__":
    main()
