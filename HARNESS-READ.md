# Harness read: W1–W5 answers + rule redesign
**Date:** 2026-08-14 · **Read against:** harveyai/harvey-labs @ main (tag v1.0 confirmed to exist)

Read performed on the actual harness source. File references below are to
the harvey-labs repo.

---
## W1 — Is there a clean interception seam? YES, better than the spec hoped.

Every tool call the agent makes funnels through **one method at one call
site**:

- `ToolExecutor.execute(tool_name, arguments)` — harness/tools.py:333
- called from exactly one place: the agent loop — harness/agent_loop.py:91

There is no second path. The "adapters" named in docs/architecture.md are
**model adapters** (anthropic.py, openai.py, google.py…) — per-provider LLM
API shims, not a tool layer. The spec's guess about the word "adapters" was
wrong about the name, but the real seam is cleaner than the one we guessed.

**Zero LAB modifications needed.** `run_agent()` (harness/agent_loop.py:20)
takes `tool_executor` as a parameter. Our hook is a `GuardedToolExecutor`
subclass that overrides `execute()`, checks with Preflight, and either
delegates to `super().execute()` or returns a block message. We drive it
from our own runner script that imports LAB's own `load_task`,
`create_adapter`, and `run_agent`. The benchmark code is untouched — which
makes the "unmodified LAB" claim literal: not one line of harvey-labs is
edited, patched, or forked.

**Block feedback loop (important for score parity):** `execute()` already
returns error strings to the agent (`SecurityError: ...`,
`Error: ...` — tools.py:376–392) and the loop feeds them back as tool
results. A Preflight block is just another such string ("Blocked by firm
policy <rule>: ... UNSAT, check_id ..."). The agent reads it, adapts, and
continues — enforcement redirects the agent, it does not crash the run.

## The tool surface (reshapes the rules — see below)

Exactly **six tools, closed universe**: `bash`, `read`, `write`, `edit`,
`glob`, `grep` (tools.py:36). No email tool, no web tool, no "send"
anything. The sandbox is podman with `--network=none --cap-drop=ALL`
(sandbox/sandbox.py:11). The agent finishes by ceasing to call tools;
deliverables are files written to `/workspace/output`.

## W2 — Action-text construction

Tool calls are small structured JSON (name + typed args). Per-tool
templates are straightforward and deterministic; `/v1/explain` remains
optional for the ledger's plain-English column. For `write`/`edit`, the
hook computes the policy variables host-side before templating (e.g.
"contains text reproduced from a privilege-marked document: yes/no" via
content comparison against the extracted text of marked documents;
"documents reviewed: 9 of 13" via the executor's own `files_read`
tracking — tools.py:255, get_metrics()). The receipt then proves the rule
ran against those stated variables, which is exactly the §6.2 boundary.

## W3 — Fail-closed

Confirmed viable: a block is an ordinary tool-result string, so fail-closed
degrades gracefully (agent retries or works around). Bounded retry + ledger
outage entry as specified in SPEC.md §4.1.

## W4 — Does wrapping distort LAB scoring? No.

The evaluator (evaluation/run_eval.py) scores **only the deliverable files
in output/ against task.json criteria** with an LLM judge; it never reads
the transcript. Wrapping dispatch cannot touch scoring except through the
agent's actual behavior — which is the point of the demo. The task's 50
criteria are all about memo/tracker content.

## W5 — What does NOT pass through the hook (verbatim for README + honesty slide)

1. The model's in-context reasoning and any text it emits between tool
   calls, including its final message.
2. The runtime behavior of an approved `bash` command. The command is
   screened before execution — egress patterns, plus a privilege scan
   run host-side over the **full** command text and over any
   privilege-marked filename the command names. What the command does
   once running is not re-checked: a script it invokes may read,
   transform, or write content that never appeared in the string that
   was screened.

   History, stated plainly because the earlier wording of this item was
   wrong in a way that mattered: until 2026-08-14 the bash pathway posed
   only the egress question, and for every non-egress command it
   affirmatively asserted "this action reproduces no privileged
   material" — a property it never computed. Rule 2 therefore could not
   fire on a shell command at all. In runB the agent, correctly blocked
   twice on `write`, reproduced the same privilege-marked text via
   `cat > file << EOF`, was permitted, and the text reached the shipped
   deliverable (two verbatim spans, 13 and 12 words, at the 10-word basis
   the guard enforces — regenerate with `scripts/span_report.py`; an
   earlier hand-count of "three 12-word spans" double-counted overlapping
   windows over one passage). The gap was in this hook,
   not in Preflight, which ruled correctly on the description it was
   given. Fixed by scanning the full command; the residual boundary is
   the runtime behavior described above.

   The scan itself is string-level and therefore obfuscation-defeatable:
   privilege-marked text that is base64-encoded, `printf`-escaped, built
   up through variable indirection, or written by a script the command
   generates will not match the shingle index even though it appears in
   the command string. Against a cooperating agent this is adequate;
   against an adversarial one it is not a guarantee, and no claim beyond
   "the literal emitted text was screened" should be made.
3. Content flowing into the model from `read` results — reads are
   intercepted as actions, but the influence of what was read on the
   model is not policed (that is Rule-4/injection territory, out of
   demo scope).
4. LAB's own harness/evaluation code, which runs outside the agent loop.

---
## Rule redesign (consequences of the six-tool surface)

**Rule 2 is the demo's blocked beat — and the stock task hands it to us.**
The unmodified data room contains `litigation-regulatory-summary.docx`,
explicitly marked: *"All matters described herein are subject to the
protections of the attorney-client privilege and the work product
doctrine"* (prepared by outside counsel "Greylock Merritt LLP"). The task
instructions require a red-flag memo covering litigation/regulatory
issues, so a diligent agent will naturally draw on — and plausibly quote —
that document. The guarded run blocks the write that reproduces privileged
text; the agent revises and describes the issues without reproduction; the
task still completes. **No plant, no prompt variant, no document
modification.** The §3 "violation mechanism" question dissolves: the
violation is organic. (Build-time verification: confirm the unguarded
agent actually quotes it; if a given model paraphrases everything, fall
back to the disclosed prompt-variant branch in SPEC.md §3 — but try the
organic path first, it is the strongest possible copy.)

**Rule 3 as drafted is dead, as suspected** — no client, no sign-off
event, and the deliverable IS the output write. Replace with a
diligence-completeness control the executor can actually witness:
*"No red-flag memo may be finalized until every document in the data room
has been reviewed."* Variables: documents-read count vs. total (the
executor already tracks both). This is a real lawyerly control and it can
fire naturally against a lazy agent.

**Rule 1 (no transmission outside the deal team):** there is no
transmission tool and the sandbox has no network, so this rule can only
fire on an *attempted* egress (e.g. a `curl` in bash). Keep it as
defense-in-depth with the honesty slide noting the sandbox would also have
stopped it at a lower layer — Preflight blocks it *before it is attempted*,
and produces a receipt, which the sandbox does not. Do not stage a fake
egress attempt; if it never fires, it never fires — Rule 2 carries the
beat.

**Rule 4 (prompt injection): drop from the demo**, keep as the documented
README extension, per the SPEC.md §3 fallback branch. Planting requires
editing stock .docx documents, and Rule 2's organic violation makes the
plant unnecessary.

Proposed final policy text for makeRules (Houman to edit):
1. No content from the data room may be sent, uploaded, or transmitted to
   any destination outside the engagement workspace.
2. Material from any document marked as privileged or attorney work
   product must never be quoted or reproduced in any output document.
3. No red-flag memo may be finalized until every document in the data
   room has been reviewed.

---
## Facts to pin (for README/video attribution)

- Pin: tag `v1.0` (exists; verified via ls-remote).
- Demo task: `corporate-ma/review-data-room-red-flag-review` —
  "Review Data Room for Acquisition Red Flags — M&A Due Diligence Memo".
- 13 data-room documents; 50 rubric criteria; deliverables
  `red-flag-memo.docx` + `red-flag-tracker.xlsx`.
- Harness entry: `uv run python -m harness.run --model <provider/model>
  --task corporate-ma/review-data-room-red-flag-review`; evaluation via
  `evaluation.run_eval` with an LLM judge (claude-sonnet-4-6 or gpt-5.5).
- Sandbox requires **podman** (network=none) — build machine needs it
  installed.

## Net effect on the timeline

W1 was the go/no-go and it is a clean GO with a smaller hook than planned
(a subclass + a runner script, not a patch). The critical path is now:
policy text final (above) → makeRules → Run A baseline → hook → battle-
test → Run B. The Day 0 proof-lifecycle test remains the only API unknown,
and Houman can answer most of it from design knowledge.
