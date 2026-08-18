# Receipts — what is in here

## Proof binaries: `<proof_id>.proof.bin` + `<proof_id>.meta.json`

Downloaded by `hook/proof_sweep.py` after each guarded run. **Proofs are
single-use by design** (anti-replay): downloading consumes them, and the
API returns 409 forever after. These archived copies are therefore the
only copies — that is their intended terminal state, not a gap.

To match a proof to the action that produced it, look up its `proof_id`
in the run's `ledger.json`. The files are flat and pooled across runs, so
the ledger is the index; the filename alone does not tell you which run a
proof came from.

**Proof coverage is 89 of 122 archived.** Ledger `proof_status` is
reconciled against the files actually on disk, so a status of
`consumed-by-download` means the binary exists in this directory, and a
run's receipt count reflects what is genuinely on hand rather than what
was requested. Every check has its `check_id` and verdict in the ledger
regardless — the decision trail is complete.

Counting note: 89 and 122 count **ledger entries** across all committed
guarded ledgers. `runB-before-bash-fix/ledger.json` is the preserved
before-picture of `runB`, and its 12 proof-bearing entries also appear
in `runB/ledger.json`, so they are counted twice; the distinct binaries
referenced by run ledgers number 77. The directory holds 80 binaries in
total: those 77, plus 2 from the pre-run live validation
(`live-validation-ledger.json`) and 1 from the day-0 gate.

Because they are consumed, **a reviewer cannot verify these**. Run your
own check and verify your own proof instead — `verify_yourself.md`.

## `probe-suite-results.json`

Output of the recording gate (`scripts/probe_suite.py`). Overwritten on
every run, so it reflects the **most recent** gate only, currently 10/10.
Each entry carries `check_id`, `proof_id`, the verbatim `action_text`,
the attempt count, and any unresolved extraction variables.

## `day0-findings.json`, `day0-*.proof.bin`

Day 0 proof-lifecycle gate: pinned the single-use semantics empirically
before any hook code existed.
