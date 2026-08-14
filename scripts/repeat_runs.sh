#!/usr/bin/env bash
# Repeat runs to separate enforcement cost from run-to-run variance.
#
# SPEC.md 2.1.3 judges parity on the SPREAD of repeated runs, not a single
# pair: observed 34, 37, 29 of 50 across three runs on 2026-08-14. One run
# per configuration cannot distinguish "the control costs ~5 criteria" from
# "the task is noisy."
#
# Cost note: Run A is unguarded and spends NO Preflight credits (agent +
# judge tokens only). Run B spends ~1 credit per governed action, ~50 per
# run, plus ~10 for the gate.
#
# Usage:
#   bash scripts/repeat_runs.sh a 3     # 3 unguarded baselines
#   bash scripts/repeat_runs.sh b 3     # 3 guarded runs (gate runs once)
set -euo pipefail

KIND="${1:?usage: repeat_runs.sh <a|b> <count>}"
COUNT="${2:?usage: repeat_runs.sh <a|b> <count>}"
TASK="${TASK:-corporate-ma/review-data-room-red-flag-review}"
AGENT_MODEL="${AGENT_MODEL:-anthropic/claude-sonnet-4-6}"
JUDGE_MODEL="${JUDGE_MODEL:-claude-sonnet-4-6}"
LAB_DIR="${LAB_DIR:-$HOME/harvey-labs}"
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY_ID="$(python3 -c "import json;print(json.load(open('$HOOK_DIR/policy/policy.json'))['policy_id'])")"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

if [ "$KIND" = "b" ]; then
  say "Battle-test gate (once for the whole batch)"
  ( cd "$HOOK_DIR" && python3 -m scripts.probe_suite ) \
    || { echo "probe suite not green — not recording"; exit 1; }
fi

# One transient stream drop must not cost the whole batch. This host has
# dropped connections repeatedly (httpx.RemoteProtocolError mid-stream);
# runB_r1 died that way and `set -e` aborted runs 2 and 3 before they
# started. Retry each run once, then carry on and report at the end.
ATTEMPTS="${ATTEMPTS:-2}"
FAILED=()

run_once() {  # $1 = run id
  local id="$1"
  if [ "$KIND" = "a" ]; then
    ( cd "$LAB_DIR" && uv run python -m harness.run \
        --model "$AGENT_MODEL" --task "$TASK" --run-id "$id" ) || return 1
  else
    ( cd "$LAB_DIR" && PYTHONPATH="$HOOK_DIR" uv run python -m hook.runner \
        --lab-root . --model "$AGENT_MODEL" --task "$TASK" \
        --policy-id "$POLICY_ID" --run-id "$id" ) || return 1
    mkdir -p "$LAB_DIR/results/$id"
    cp -r "$HOOK_DIR/runs/guarded/$id/output" "$LAB_DIR/results/$id/" 2>/dev/null || true
    cp "$HOOK_DIR/runs/guarded/$id/metrics.json" "$LAB_DIR/results/$id/" 2>/dev/null || true
  fi
  ( cd "$LAB_DIR" && uv run python -m evaluation.run_eval \
      --run-id "$id" --task "$TASK" --judge-model "$JUDGE_MODEL" ) || return 1
}

for i in $(seq 1 "$COUNT"); do
  id="run${KIND^^}_r$i"
  ok=0
  for attempt in $(seq 1 "$ATTEMPTS"); do
    say "RUN ${KIND^^} $i/$COUNT ($id) attempt $attempt/$ATTEMPTS"
    if run_once "$id"; then ok=1; break; fi
    say "$id attempt $attempt failed (likely a dropped stream) — retrying"
    sleep 20
  done
  [ "$ok" = 1 ] || { say "$id FAILED after $ATTEMPTS attempts"; FAILED+=("$id"); }
done

if [ ${#FAILED[@]} -gt 0 ]; then
  say "Batch finished with failures: ${FAILED[*]}"
fi

say "Batch done — summarise with: python3 -m scripts.summarize_runs"
