"""Post-run proof sweep.

Proofs generate asynchronously (30–60s), so the guard only queues proof
ids during the run. This sweep polls each proof until ready and
downloads the binary into receipts/. Downloading CONSUMES the proof
(single-use, by design — anti-replay); the ledger records
"consumed-by-download" as the intended terminal state for archived
proofs. See SPEC.md §6.3 — third parties verify their own fresh proofs
via verify_yourself.md, not these archived ones.
"""

import json
from pathlib import Path

from hook.ledger import load_entries
from hook.preflight_client import (PreflightClient, PreflightError,
                                   PreflightHTTPError)


def sweep(ledger_jsonl: str | Path, receipts_dir: str | Path,
          client: PreflightClient, ledger_json_out: str | Path | None = None,
          poll_s: float = 5.0, timeout_s: float = 120.0) -> list[dict]:
    receipts = Path(receipts_dir)
    receipts.mkdir(parents=True, exist_ok=True)
    entries = load_entries(ledger_jsonl)

    for e in entries:
        proof_id = e.get("proof_id")
        if not proof_id or e.get("proof_status") not in (None, "pending"):
            continue
        try:
            meta = client.wait_for_proof(proof_id, poll_s=poll_s,
                                         timeout_s=timeout_s)
            blob = client.proof_download(proof_id)
            out = receipts / f"{proof_id}.proof.bin"
            out.write_bytes(blob)
            (receipts / f"{proof_id}.meta.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8")
            e["proof_status"] = "consumed-by-download"
            e["proof_file"] = out.name
            print(f"  archived {proof_id} ({len(blob)} bytes)")
        except PreflightHTTPError as ex:
            e["proof_status"] = f"failed:http-{ex.status}"
            print(f"  FAILED {proof_id}: HTTP {ex.status}")
        except PreflightError as ex:
            e["proof_status"] = f"failed:{ex}"
            print(f"  FAILED {proof_id}: {ex}")

    if ledger_json_out:
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["result"]] = counts.get(e["result"], 0) + 1
        Path(ledger_json_out).write_text(json.dumps({
            "totals": counts, "entries": entries}, indent=2),
            encoding="utf-8")
    return entries
