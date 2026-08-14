"""What does loosening Rule 2's shingle threshold cost in detection?

The parity diagnostic suggests SHINGLE_WORDS=10 suppresses legitimate
figure reporting: 4 of the 5 criteria lost under guard turn on exact
figures ($263,000 prepayment premium, $1.8M asbestos add-back, total debt,
EBITDA itemisation), which are hard to state without overlapping the
source. The obvious response is to raise the threshold.

Before paying for a run at a looser setting, ask the cheaper question:
would a looser guard still have caught the leaks we actually observed?
Every span the ungoverned runs put into the client deliverable is already
on disk, so this is answerable offline and for free.

Reports, per candidate threshold, how many of those real leaks remain
detectable. A threshold that scores 0 on the unguarded runs is a guard
that would have permitted every leak in this data set.

Usage: python3 -m scripts.shingle_sweep [--max 20]
Writes receipts/shingle-sweep.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hook.privileged import (SHINGLE_WORDS, PrivilegedIndex,  # noqa: E402
                             _extract_text, _words, is_scannable)
from scripts.span_report import deliverables, resolve_docs_dir  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def marked_shingles_at(idx_dir: Path, n: int) -> set[str]:
    """n-word shingles over every privilege-marked document."""
    idx = PrivilegedIndex(idx_dir)
    out: set[str] = set()
    for name in idx.marked_names:
        words = _words(_extract_text(idx_dir / name))
        out |= {" ".join(words[i:i + n])
                for i in range(max(0, len(words) - n + 1))}
    return out


def spans_at(text: str, marked: set[str], n: int) -> list[int]:
    words = _words(text)
    total = len(words)
    if total < n:
        return []
    covered = [False] * total
    for i in range(total - n + 1):
        if " ".join(words[i:i + n]) in marked:
            for j in range(i, i + n):
                covered[j] = True
    spans, i = [], 0
    while i < total:
        if covered[i]:
            j = i
            while j < total and covered[j]:
                j += 1
            spans.append(j - i)
            i = j
        else:
            i += 1
    return spans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--docs-dir", default=None)
    args = ap.parse_args()

    docs_dir = resolve_docs_dir(args.docs_dir)
    runs = []
    for arm in ("unguarded", "guarded"):
        root = REPO / "runs" / arm
        if not root.exists():
            continue
        for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (run_dir / "metrics.json").exists():
                continue
            text = "".join(_extract_text(f)
                           for f in deliverables(run_dir, False)
                           if is_scannable(f))
            runs.append((f"{arm}/{run_dir.name}", arm, text))

    rows = []
    for n in range(SHINGLE_WORDS, args.max + 1):
        marked = marked_shingles_at(docs_dir, n)
        per_run = {}
        for label, arm, text in runs:
            per_run[label] = len(spans_at(text, marked, n))
        leaks_caught = sum(v for k, v in per_run.items()
                           if k.startswith("unguarded"))
        rows.append({"shingle_words": n, "spans_per_run": per_run,
                     "unguarded_spans_detected": leaks_caught})

    base = rows[0]["unguarded_spans_detected"]
    print(f"Real leaks in the ungoverned deliverables, as seen at each "
          f"candidate threshold\n(baseline: {base} spans at the shipped "
          f"SHINGLE_WORDS={SHINGLE_WORDS})\n")
    print(f"{'threshold':>9}  {'leaks detected':>14}  {'retained':>8}")
    print(f"{'-'*9}  {'-'*14}  {'-'*8}")
    for r in rows:
        pct = 100 * r["unguarded_spans_detected"] / base if base else 0
        flag = "  <-- BLIND" if r["unguarded_spans_detected"] == 0 else ""
        print(f"{r['shingle_words']:>9}  {r['unguarded_spans_detected']:>14}"
              f"  {pct:>7.0f}%{flag}")

    out = REPO / "receipts" / "shingle-sweep.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat(),
        "shipped_shingle_words": SHINGLE_WORDS,
        "documents_dir": str(docs_dir),
        "note": "Detection only. Raising the threshold also reduces false "
                "positives on legitimate figure reporting, which this does "
                "not measure — that needs a guarded run at the new setting.",
        "thresholds": rows,
    }, indent=2), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    main()
