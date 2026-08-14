#!/usr/bin/env bash
# Set up an Ubuntu machine (native or WSL2) and run the Preflight-in-LAB
# demo end to end: Run A (unguarded baseline) and Run B (guarded).
#
# Prereqs you provide (export before running, or the script will prompt):
#   ANTHROPIC_API_KEY   - the agent-under-test AND the LAB judge
#   PREFLIGHT_API_KEY   - your ICME key (sk-smt-...); 444 credits as of 2026-08-14
# The compiled policy id is read from policy/policy.json in this repo.
#
# Usage:
#   git clone https://github.com/hshadab/harvey.git && cd harvey
#   export ANTHROPIC_API_KEY=...   PREFLIGHT_API_KEY=...
#   bash scripts/setup_and_run.sh            # setup + Run A + score A
#   bash scripts/setup_and_run.sh --run-b    # also Run B (guarded) + score B
#   bash scripts/setup_and_run.sh --all      # A and B in one go
set -euo pipefail

# ── config (override via env) ─────────────────────────────────────────
LAB_TAG="${LAB_TAG:-v1.0}"
TASK="${TASK:-corporate-ma/review-data-room-red-flag-review}"
AGENT_MODEL="${AGENT_MODEL:-anthropic/claude-sonnet-4-6}"   # cheapest that
                                     # completes the long-horizon task well;
                                     # swap to a haiku-class id to spend less
JUDGE_MODEL="${JUDGE_MODEL:-claude-sonnet-4-6}"
LAB_DIR="${LAB_DIR:-$HOME/harvey-labs}"
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DO_A=1; DO_B=0
case "${1:-}" in
  --run-b) DO_A=0; DO_B=1 ;;
  --all)   DO_A=1; DO_B=1 ;;
  --run-a|"") : ;;
  *) echo "usage: $0 [--run-a|--run-b|--all]"; exit 2 ;;
esac

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1; }

# ── keys ──────────────────────────────────────────────────────────────
: "${ANTHROPIC_API_KEY:?export ANTHROPIC_API_KEY first}"
: "${PREFLIGHT_API_KEY:?export PREFLIGHT_API_KEY first}"
POLICY_ID="$(python3 -c "import json;print(json.load(open('$HOOK_DIR/policy/policy.json'))['policy_id'])")"
say "Policy: $POLICY_ID   Agent: $AGENT_MODEL   Task: $TASK"

# ── dependencies ──────────────────────────────────────────────────────
if ! need podman || ! need pandoc; then
  say "Installing podman + pandoc (sudo)"
  # pandoc is REQUIRED for scoring: without it LAB's judge cannot open the
  # .docx deliverable, reads the converter error as the memo's content, and
  # returns a confident false ~7/50 instead of erroring (observed
  # 2026-08-14; see the Defect-1 note in BATTLE-TEST-FINDINGS.md).
  sudo apt-get update -y && sudo apt-get install -y podman pandoc
fi
if ! need uv; then
  say "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
podman info >/dev/null 2>&1 || { echo "podman not working; on WSL2 ensure systemd is on"; exit 1; }

# ── pin LAB ───────────────────────────────────────────────────────────
if [ ! -d "$LAB_DIR/.git" ]; then
  say "Cloning harvey-labs @ $LAB_TAG"
  GIT_LFS_SKIP_SMUDGE=1 git clone --branch "$LAB_TAG" --depth 1 \
    https://github.com/harveyai/harvey-labs.git "$LAB_DIR"
fi

run_id_file="$HOOK_DIR/.last_run_id"

# ── Run A: unguarded baseline (LAB's own CLI, untouched) ──────────────
if [ "$DO_A" = 1 ]; then
  say "RUN A — unguarded baseline"
  ( cd "$LAB_DIR"
    ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
      uv run python -m harness.run --model "$AGENT_MODEL" --task "$TASK" \
      --run-id "runA" )
  say "Scoring Run A"
  ( cd "$LAB_DIR"
    ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
      uv run python -m evaluation.run_eval --run-id "runA" --task "$TASK" \
      --judge-model "$JUDGE_MODEL" )
  cp -r "$LAB_DIR/results/runA" "$HOOK_DIR/runs/unguarded/" 2>/dev/null || true
fi

# ── Run B: guarded (LAB imported as a library; zero LAB edits) ────────
if [ "$DO_B" = 1 ]; then
  say "Battle-test gate — all pathways must be green before recording"
  ( cd "$HOOK_DIR" && PREFLIGHT_API_KEY="$PREFLIGHT_API_KEY" \
      python3 -m scripts.probe_suite ) || {
        echo "probe suite not green — fix before Run B"; exit 1; }

  say "RUN B — guarded"
  ( cd "$LAB_DIR"
    PYTHONPATH="$HOOK_DIR" \
    ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    PREFLIGHT_API_KEY="$PREFLIGHT_API_KEY" \
      uv run python -m hook.runner --lab-root . --model "$AGENT_MODEL" \
      --task "$TASK" --policy-id "$POLICY_ID" --run-id "runB" )

  # Score Run B with LAB's evaluator, pointing it at the guarded output.
  say "Scoring Run B"
  gb="$HOOK_DIR/runs/guarded/runB"
  mkdir -p "$LAB_DIR/results/runB"
  cp -r "$gb/output" "$LAB_DIR/results/runB/" 2>/dev/null || true
  cp "$gb/metrics.json" "$LAB_DIR/results/runB/" 2>/dev/null || true
  ( cd "$LAB_DIR"
    ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
      uv run python -m evaluation.run_eval --run-id "runB" --task "$TASK" \
      --judge-model "$JUDGE_MODEL" )
fi

say "Done"
echo "Run A results:   $LAB_DIR/results/runA/   (+ copy in runs/unguarded/)"
echo "Run B results:   $HOOK_DIR/runs/guarded/runB/"
echo "Receipt ledger:  $HOOK_DIR/runs/guarded/runB/ledger.md"
echo "Proofs:          $HOOK_DIR/receipts/"
echo "Compare the two LAB scores for parity (SPEC.md 2.1.3)."
