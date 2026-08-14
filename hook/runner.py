"""Guarded run — LAB's own harness with the Preflight guard wrapped
around its tool executor.

Run A (unguarded baseline) uses LAB's stock CLI, untouched:
    cd $HARVEY_LABS_ROOT && uv run python -m harness.run \
        --model <provider/model> --task corporate-ma/review-data-room-red-flag-review

Run B (guarded) uses this module, which imports LAB's harness as a
library — zero modifications to harvey-labs:
    PREFLIGHT_API_KEY=... uv run python -m hook.runner \
        --lab-root /path/to/harvey-labs \
        --model <provider/model> \
        --task corporate-ma/review-data-room-red-flag-review \
        --policy-id <compiled policy id>

The setup below mirrors harness/run.py main() step for step; the only
functional difference is GuardedExecutor wrapping the ToolExecutor, plus
the receipt ledger and post-run proof sweep.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def add_lab_to_path(lab_root: str) -> Path:
    root = Path(lab_root).resolve()
    if not (root / "harness" / "run.py").exists():
        raise SystemExit(f"not a harvey-labs checkout: {root}")
    sys.path.insert(0, str(root))
    return root


def main():
    p = argparse.ArgumentParser(description="Guarded LAB run (Preflight)")
    p.add_argument("--lab-root",
                   default=os.environ.get("HARVEY_LABS_ROOT"),
                   help="Path to the harvey-labs checkout (pinned tag)")
    p.add_argument("--model", required=True)
    p.add_argument("--task",
                   default="corporate-ma/review-data-room-red-flag-review")
    p.add_argument("--policy-id",
                   default=os.environ.get("PREFLIGHT_POLICY_ID"),
                   help="Compiled Preflight policy id")
    p.add_argument("--mode", choices=["demo", "production"], default="demo",
                   help="demo checks every intercepted call; production "
                        "pre-screens with the free relevance endpoint")
    p.add_argument("--run-id", default=None)
    p.add_argument("--max-turns", type=int, default=200)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--shell-timeout", type=int, default=60)
    p.add_argument("--reasoning-effort", default=None)
    p.add_argument("--skills", nargs="*", default=None)
    p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs" / "guarded"))
    p.add_argument("--receipts-dir", default=str(REPO_ROOT / "receipts"))
    args = p.parse_args()

    if not args.lab_root:
        raise SystemExit("--lab-root or HARVEY_LABS_ROOT is required")
    if not args.policy_id:
        raise SystemExit("--policy-id or PREFLIGHT_POLICY_ID is required")

    add_lab_to_path(args.lab_root)

    # LAB imports (after sys.path setup).
    from harness.agent_loop import run_agent
    from harness.run import (DEFAULT_SKILLS, SYSTEM_PROMPT_PREAMBLE,
                             _load_env, create_adapter, load_skills,
                             load_task, setup_skill_scripts)
    from harness.tools import ToolExecutor, get_all_tool_definitions
    from sandbox.sandbox import DEFAULT_IMAGE, Sandbox

    from hook.guard import GuardConfig, GuardedExecutor
    from hook.ledger import render_markdown
    from hook.preflight_client import PreflightClient
    from hook.proof_sweep import sweep

    _load_env()
    client = PreflightClient()

    if args.run_id is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.run_id = f"{ts}-{args.model.split('/')[-1].replace('.', '-')}"

    task = load_task(task_name=args.task)
    deliverables = list(task["config"].get("deliverables", {}).keys())

    results_dir = Path(args.runs_dir) / args.run_id
    output_dir = results_dir / "output"
    workspace_dir = results_dir / "workspace"
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    sandbox = Sandbox(
        documents_dir=Path(task["docs_dir"]),
        output_dir=output_dir,
        workspace_dir=workspace_dir,
        image=DEFAULT_IMAGE,
        default_timeout=args.shell_timeout,
    )
    sandbox.start()
    print(f"Sandbox up (documents={sandbox.documents_dir})")

    adapter = create_adapter(model=args.model, temperature=args.temperature,
                             reasoning_effort=args.reasoning_effort)
    inner = ToolExecutor(sandbox=sandbox, shell_timeout=args.shell_timeout)

    guard = GuardedExecutor(inner, client, GuardConfig(
        policy_id=args.policy_id,
        documents_dir=task["docs_dir"],
        deliverable_names=deliverables,
        ledger_path=str(results_dir / "ledger.jsonl"),
        mode=args.mode,
    ))
    print(f"Guard up: policy {args.policy_id}, mode {args.mode}, "
          f"deliverables {deliverables}, "
          f"privilege-marked docs: {guard._privileged.marked_names}")

    skill_names = DEFAULT_SKILLS if args.skills is None else args.skills
    system_prompt = SYSTEM_PROMPT_PREAMBLE
    if skill_names:
        system_prompt += load_skills(skill_names)
        setup_skill_scripts(skill_names, workspace_dir)

    (results_dir / "config.json").write_text(json.dumps({
        "model": args.model, "task": args.task, "run_id": args.run_id,
        "policy_id": args.policy_id, "mode": args.mode,
        "max_turns": args.max_turns, "temperature": args.temperature,
        "skills": skill_names,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    try:
        result = run_agent(
            adapter=adapter,
            system_prompt=system_prompt,
            user_prompt=task["instructions"],
            tool_executor=guard,
            tools=get_all_tool_definitions(),
            max_turns=args.max_turns,
            transcript_path=str(results_dir / "transcript.jsonl"),
        )
    finally:
        sandbox.stop()
        guard.finish(results_dir / "ledger.json")

    (results_dir / "metrics.json").write_text(json.dumps({
        **{k: result[k] for k in ("turn_count", "input_tokens",
                                  "output_tokens", "wall_clock_seconds",
                                  "finished_cleanly")},
        **result["tool_metrics"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    print("\nProof sweep (proofs generate in 30-60s; polling)...")
    entries = sweep(results_dir / "ledger.jsonl", args.receipts_dir, client,
                    ledger_json_out=results_dir / "ledger.json")
    (results_dir / "ledger.md").write_text(render_markdown(entries),
                                           encoding="utf-8")

    sat = sum(1 for e in entries if e["result"] == "SAT")
    unsat = sum(1 for e in entries if e["result"] == "UNSAT")
    print(f"\nGuarded run complete: {results_dir}")
    print(f"  checks: {len(entries)}  SAT: {sat}  UNSAT (blocked): {unsat}")
    print(f"  ledger: {results_dir / 'ledger.md'}")
    print(f"  receipts: {args.receipts_dir}")
    print("\nScore it with LAB's own evaluator, e.g.:")
    print(f"  cd {args.lab_root} && uv run python -m evaluation.run_eval "
          f"--run-id <id> --task {args.task} --judge-model claude-sonnet-4-6")


if __name__ == "__main__":
    main()
