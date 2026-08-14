"""Day 0 gate: empirically pin down the proof lifecycle before any hook
code is trusted (SPEC.md §7).

Runs two throwaway checks (~2 credits) against an existing policy and
answers, from observed behavior:
  (a) exact response field names on checkItProd
  (b) proof readiness timing vs the documented 30-60s
  (c) both consumption orderings:
        check A: download first, then verifyProof  -> expect 409 on verify?
        check B: verifyProof first, then download  -> expect 409 on download?
  (d) what verifyProof returns (valid / policy_hash / claimed_result / used)

Findings are printed and saved to receipts/day0-findings.json. Update
hook/preflight_client.py docstrings if anything differs.

Usage:
  PREFLIGHT_API_KEY=... python -m scripts.day0_proof_lifecycle [--policy-id X]
  (defaults to the policy in policy/policy.json, else the account's first)
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hook.preflight_client import (PreflightClient,  # noqa: E402
                                   PreflightHTTPError)

REPO = Path(__file__).resolve().parent.parent
RECEIPTS = REPO / "receipts"

THROWAWAY_ACTION = (
    "The agent reads the document test-note.txt inside the engagement "
    "workspace for review purposes. The action does not transmit any "
    "content outside the engagement workspace: the transmission attempt "
    "is no. No privileged material is reproduced and no final "
    "deliverable is written. Therefore this read is permitted."
)


def pick_policy(client, arg_policy):
    if arg_policy:
        return arg_policy
    pj = REPO / "policy" / "policy.json"
    if pj.exists():
        return json.loads(pj.read_text())["policy_id"]
    pols = client.my_policies()
    items = pols.get("policies") or pols.get("items") or []
    if not items:
        raise SystemExit("no policy available — compile one first "
                         "(scripts/compile_policy.py) or pass --policy-id")
    first = items[0]
    return first.get("policy_id") or first.get("id")


def run_check(client, policy_id, label):
    t0 = time.monotonic()
    res = client.check_it_prod(policy_id, THROWAWAY_ACTION)
    print(f"\n[{label}] checkItProd in {time.monotonic()-t0:.2f}s; "
          f"fields: {sorted(res.keys())}")
    print(json.dumps(res, indent=2))
    return res


def wait_ready(client, proof_id):
    t0 = time.monotonic()
    meta = client.wait_for_proof(proof_id)
    dt = time.monotonic() - t0
    print(f"  proof ready after {dt:.1f}s; meta fields: {sorted(meta.keys())}")
    return dt


def try_op(name, fn):
    try:
        out = fn()
        preview = f"{len(out)} bytes" if isinstance(out, bytes) \
            else json.dumps(out)
        print(f"  {name}: OK -> {preview}")
        return {"ok": True, "result": preview if isinstance(out, bytes)
                else out}
    except PreflightHTTPError as e:
        print(f"  {name}: HTTP {e.status} -> {e.payload or e.body[:200]}")
        return {"ok": False, "status": e.status,
                "body": e.payload or str(e.body[:200])}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--policy-id")
    args = p.parse_args()
    client = PreflightClient()
    policy_id = pick_policy(client, args.policy_id)
    print(f"policy: {policy_id}")
    RECEIPTS.mkdir(exist_ok=True)
    findings = {"at": datetime.now(timezone.utc).isoformat(),
                "policy_id": policy_id}

    # ── Check A: download first, then verify ─────────────────────────
    res_a = run_check(client, policy_id, "A")
    findings["checkItProd_fields"] = sorted(res_a.keys())
    proof_a = res_a.get("proof_id") or res_a.get("zk_proof_id")
    if proof_a:
        findings["proof_ready_seconds_A"] = wait_ready(client, proof_a)
        blob: dict = {}

        def _download():
            blob["bytes"] = client.proof_download(proof_a)
            return blob["bytes"]

        dl = try_op("download", _download)
        if dl["ok"] and blob.get("bytes"):
            (RECEIPTS / f"day0-{proof_a}.proof.bin").write_bytes(
                blob["bytes"])
        ver = try_op("verifyProof after download",
                     lambda: client.verify_proof(proof_a))
        meta2 = try_op("metadata after consumption",
                       lambda: client.proof_meta(proof_a))
        findings["order_download_then_verify"] = {
            "download": dl, "verify": ver, "meta_after": meta2}
    else:
        print("  no proof_id on check A!")
        findings["order_download_then_verify"] = "no proof_id returned"

    # ── Check B: verify first, then download ─────────────────────────
    res_b = run_check(client, policy_id, "B")
    proof_b = res_b.get("proof_id") or res_b.get("zk_proof_id")
    if proof_b:
        findings["proof_ready_seconds_B"] = wait_ready(client, proof_b)
        ver = try_op("verifyProof", lambda: client.verify_proof(proof_b))
        dl = try_op("download after verify",
                    lambda: client.proof_download(proof_b))
        findings["order_verify_then_download"] = {
            "verify": ver, "download": dl}
    else:
        findings["order_verify_then_download"] = "no proof_id returned"

    out = RECEIPTS / "day0-findings.json"
    out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"\nFindings saved: {out}")
    print("Update hook/preflight_client.py notes if anything above "
          "contradicts the documented behavior.")


if __name__ == "__main__":
    main()
