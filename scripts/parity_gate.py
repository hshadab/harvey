"""The parity gate — SPEC.md §2.1.3, made mechanical.

Collects every scored run in the repo (runs/unguarded/*/scores.json and
runs/guarded/*/scores.json, plus any extra directories passed as
arguments), applies the decision rule, and writes
receipts/parity-report.json.

Decision rule (SPEC §2.1.3):
- Metric is n_passed out of n_criteria — never the all-or-nothing
  headline `score`.
- Parity is judged on the SPREAD of repeated runs, not a single pair:
  * overlap AND guarded median >= unguarded min -> PARITY-PLAUSIBLE
  * overlap BUT guarded median < unguarded min  -> PARITY-MARGINAL
  * guarded max < unguarded min                 -> PARITY-FAIL
  * fewer than 2 valid runs per arm             -> INCONCLUSIVE

  The median condition exists because overlap alone is too weak: one
  strong guarded run touching the baseline floor satisfies it while every
  other guarded run sits below. Requiring the median means the TYPICAL
  guarded run carries the verdict, not the best one. MARGINAL is not a
  pass — it exits non-zero and must be reported as "no measurable
  degradation at this sample size", never as parity.
- A scoring is EXCLUDED as corrupted if a majority of its criteria
  reasonings cite a converter failure rather than memo content (the
  pandoc artifact: the judge reads the .docx conversion error as the
  memo and fails everything — Defect 1, BATTLE-TEST-FINDINGS.md).

Diagnostic: criteria that pass in most unguarded runs but fail in most
guarded runs are listed by id/title. If those cluster on criteria whose
substance requires near-verbatim references (instrument names, dates),
the gap is the Rule 2 shingle proxy over-triggering, not the control
itself — that is a template/threshold fix, not an accepted cost.

Usage:
    python3 -m scripts.parity_gate [extra_results_dir ...]
Exit code: 0 for PARITY-PLAUSIBLE, 1 otherwise.
"""

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CORRUPTION_MARKERS = ("pandoc", "could not read the file",
                      "unable to convert", "conversion failed")


def classify_arm(scores_path: Path, run_id: str | None) -> str:
    """Which arm a scored run belongs to.

    run_id is authoritative and path is the fallback. Inferring from the
    path alone was wrong for the documented workflow: repeats are scored in
    the LAB checkout (results/runB_r1/scores.json), whose path contains no
    'guarded' component, so every guarded repeat was counted as UNGUARDED —
    silently polluting the baseline and leaving the guarded arm empty
    (observed 2026-08-17: results/runB/scores.json, the 29/50 guarded run,
    classified unguarded).
    """
    rid = (run_id or "").lower()
    if rid.startswith("runb"):
        return "guarded"
    if rid.startswith("runa"):
        return "unguarded"
    parts = str(scores_path).replace("\\", "/").split("/")
    return "guarded" if "guarded" in parts else "unguarded"


def load_scored_runs(extra_dirs: list[str]) -> list[dict]:
    roots = [REPO / "runs" / "unguarded", REPO / "runs" / "guarded"]
    roots += [Path(d) for d in extra_dirs]
    runs = []
    for root in roots:
        if not root.exists():
            continue
        for scores in sorted(root.rglob("scores.json")):
            d = json.loads(scores.read_text(encoding="utf-8"))
            crits = d.get("criteria_results") or []
            corrupted = [c for c in crits
                         if any(m in (c.get("reasoning") or "").lower()
                                for m in CORRUPTION_MARKERS)]
            arm = classify_arm(scores, d.get("run_id"))
            runs.append({
                "path": str(scores.relative_to(REPO)) if scores.is_relative_to(REPO) else str(scores),
                "arm": arm,
                "run_id": d.get("run_id"),
                "n_passed": d.get("n_passed"),
                "n_criteria": d.get("n_criteria"),
                "scored_at": d.get("scored_at"),
                "judge_model": d.get("judge_model"),
                "corrupted": len(corrupted) > len(crits) / 2 if crits else False,
                "corrupted_count": len(corrupted),
                "criteria": {c["id"]: c.get("verdict") == "pass"
                             for c in crits},
                "titles": {c["id"]: c.get("title") for c in crits},
            })
    return runs


def majority_pass(runs: list[dict], cid: str) -> bool | None:
    votes = [r["criteria"][cid] for r in runs if cid in r["criteria"]]
    if not votes:
        return None
    return sum(votes) > len(votes) / 2


def dedupe(runs: list[dict]) -> list[dict]:
    """One entry per run_id, keeping the most recently scored copy.

    The same run is reachable twice — once in runs/ inside the repo, once
    in the LAB checkout it was scored in — which silently inflated n and
    would skew any mean (observed: runA counted twice in the unguarded
    arm). Later scored_at wins, so a re-score supersedes a stale copy.
    """
    best: dict[str, dict] = {}
    for r in runs:
        key = r.get("run_id") or r["path"]
        prev = best.get(key)
        if prev is None or (r.get("scored_at") or "") > (prev.get("scored_at") or ""):
            best[key] = r
    return sorted(best.values(), key=lambda r: (r["arm"], r.get("run_id") or ""))


def main():
    runs = dedupe(load_scored_runs(sys.argv[1:]))
    valid = [r for r in runs if not r["corrupted"]]
    excluded = [r for r in runs if r["corrupted"]]

    for r in excluded:
        print(f"EXCLUDED (corrupted scoring, {r['corrupted_count']} "
              f"converter-failure criteria): {r['path']} "
              f"[n_passed={r['n_passed']}]")

    arms = {"unguarded": [], "guarded": []}
    for r in valid:
        arms[r["arm"]].append(r)

    print("\nValid scored runs:")
    for arm, rs in arms.items():
        for r in rs:
            print(f"  {arm:9s} {r['n_passed']}/{r['n_criteria']}  "
                  f"{r['path']}")
        if not rs:
            print(f"  {arm:9s} (none)")

    ug = sorted(r["n_passed"] for r in arms["unguarded"])
    gd = sorted(r["n_passed"] for r in arms["guarded"])

    # Range overlap alone is too weak to call parity: a single strong
    # guarded run touching the bottom of the baseline satisfies it while
    # every other guarded run sits below. Observed 2026-08-17 — guarded
    # 29/33/36 against unguarded 34/34/39 "passed" on the 36 alone, with a
    # mean 3.0 criteria lower. PLAUSIBLE therefore also requires the
    # guarded MEDIAN to reach the baseline floor, so the typical guarded
    # run — not the best one — carries the verdict.
    med = statistics.median
    if len(ug) < 2 or len(gd) < 2:
        verdict = "INCONCLUSIVE"
        reason = (f"need >=2 valid scored runs per arm "
                  f"(have {len(ug)} unguarded, {len(gd)} guarded)")
    elif gd[-1] < ug[0]:
        verdict = "PARITY-FAIL"
        reason = (f"guarded range {gd[0]}-{gd[-1]} lies entirely below "
                  f"unguarded range {ug[0]}-{ug[-1]}")
    elif med(gd) < ug[0]:
        verdict = "PARITY-MARGINAL"
        reason = (f"ranges overlap (unguarded {ug[0]}-{ug[-1]}, guarded "
                  f"{gd[0]}-{gd[-1]}) but the guarded median {med(gd):g} "
                  f"is below the unguarded floor {ug[0]}: only "
                  f"{sum(1 for x in gd if x >= ug[0])} of {len(gd)} guarded "
                  f"runs reach it, and the means differ by "
                  f"{statistics.mean(ug) - statistics.mean(gd):.1f} "
                  f"criteria. Overlap here rests on the best guarded run, "
                  f"not the typical one — report it as such, do not claim "
                  f"parity")
    else:
        verdict = "PARITY-PLAUSIBLE"
        reason = (f"ranges overlap and the guarded median {med(gd):g} "
                  f"reaches the unguarded floor {ug[0]} "
                  f"(unguarded {ug[0]}-{ug[-1]}, guarded {gd[0]}-{gd[-1]})")

    # Diagnostics. Two tiers, because they mean different things:
    #
    # SYSTEMATIC — always passes unguarded AND never passes guarded. This
    # is the fingerprint of a real quality cost. (At n=3/n=4 on
    # 2026-08-17: ZERO criteria met it.)
    #
    # MAJORITY — passes most unguarded runs, fails most guarded runs.
    # Weak evidence only: criteria flip freely WITHIN arms (15/50 across
    # the unguarded runs, 23/50 across the guarded), so a majority split
    # on small n is usually that same noise landing unevenly. Earlier
    # readings of this list as "criteria the guard costs" — including the
    # figure-criteria diagnosis that motivated the de-minimis change —
    # overread it: the de-minimis run recovered 3 of those 5 criteria yet
    # scored 31, and two zero-block guarded runs differ by 5 criteria.
    lost, systematic = [], []
    flips = {}
    if arms["unguarded"] and arms["guarded"]:
        all_ids = sorted({cid for r in valid for cid in r["criteria"]})
        for arm_name, rs in arms.items():
            flips[arm_name] = sum(
                1 for cid in all_ids
                if len({r["criteria"].get(cid) for r in rs
                        if cid in r["criteria"]}) > 1)
        for cid in all_ids:
            uv = [r["criteria"][cid] for r in arms["unguarded"]
                  if cid in r["criteria"]]
            gv = [r["criteria"][cid] for r in arms["guarded"]
                  if cid in r["criteria"]]
            title = next((r["titles"].get(cid) for r in valid
                          if r["titles"].get(cid)), "")
            if uv and gv and all(uv) and not any(gv):
                systematic.append({"id": cid, "title": title})
            u, g = majority_pass(arms["unguarded"], cid), \
                majority_pass(arms["guarded"], cid)
            if u is True and g is False:
                lost.append({"id": cid, "title": title})

    print(f"\nVERDICT: {verdict} — {reason}")
    if flips:
        print(f"\nWithin-arm flip rates (criteria that change verdict "
              f"between runs of the SAME arm): "
              + ", ".join(f"{k} {v}/50" for k, v in flips.items()))
    print(f"\nSYSTEMATIC cost signature (always-pass unguarded, "
          f"never-pass guarded): {len(systematic)} criteria"
          + (" — none: no criterion consistently separates the arms"
             if not systematic else ""))
    for c in systematic:
        print(f"  {c['id']}: {c['title']}")
    if lost:
        print(f"\nMajority-split criteria ({len(lost)}) — weak evidence "
              f"given the flip rates above; do not read as a stable "
              f"deficit list:")
        for c in lost:
            print(f"  {c['id']}: {c['title']}")

    out = REPO / "receipts" / "parity-report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict, "reason": reason,
        "unguarded_n_passed": ug, "guarded_n_passed": gd,
        "within_arm_flips": flips,
        "systematic_cost_criteria": systematic,
        "criteria_lost_under_guard": lost,
        "excluded_corrupted": [r["path"] for r in excluded],
        "runs": [{k: r[k] for k in ("path", "arm", "run_id", "n_passed",
                                    "n_criteria", "scored_at",
                                    "corrupted")} for r in runs],
    }, indent=2), encoding="utf-8")
    print(f"\nReport: {out}")
    sys.exit(0 if verdict == "PARITY-PLAUSIBLE" else 1)


if __name__ == "__main__":
    main()
