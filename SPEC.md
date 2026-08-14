# Spec: Preflight enforcement inside Harvey LAB
**Version:** v2 (2026-08-14)

**Changes in v2** (from review against the live docs.icme.io API reference):
- §6.3 rewritten around single-use proof semantics (by design, anti-replay):
  committed verification story is now **verify-it-yourself live**; offline
  verification is an upgrade path, not an assumption.
- §2.1 adds a score-parity success criterion for the guarded run.
- §3 resolves the Rule 3 observability problem and commits to a violation
  mechanism for the no-Rule-4 branch now, not at build time.
- §4.1 pseudocode fixed for async proof generation (30–60s), bounded-retry
  fail-closed, and check-everything demo mode (relevance screen is the
  production pattern, not the recorded pattern).
- Field names corrected to `proof_id` / `proof_url` per API reference.
- §7 Day 0/1 adds the proof-lifecycle empirical test as a gate.
- §1.1 LAB figures flagged for re-pin from the pinned tag at build time.

---
## 1. Background
### 1.1 What Harvey LAB is
Harvey LAB (Legal Agent Benchmark) is Harvey AI's open-source benchmark for
evaluating LLM agents on legal work. Repo: github.com/harveyai/harvey-labs.
MIT licensed.

NOTE (v2): public figures drift — Harvey's launch materials say "1,200+
tasks, 75,000+ rubric criteria"; the repo README may say more; star counts
move weekly. Every LAB number that appears in any artifact gets pulled from
the **pinned tag** at build time (see §8), not from memory or this spec.
Confirm the pin target tag actually exists before writing it into the
README.

Components relevant to us:
- **tasks/** — tasks across 24 practice areas plus contracting. Each
  task bundles agent instructions, documents, and a scoring rubric.
- **harness/** — the execution harness that runs an agent against a task:
  provisions the environment, exposes tools, records the run.
- **sandbox/** — the isolated environment agents execute in.
- **evaluation/** — all-pass rubric scoring plus an LLM judge
  (docs/eval-strategies.md).
- **docs/architecture.md** — describes the task model, harness, tools,
  adapters, reports, and sweeps. The word "adapters" is the reason this
  spec exists: it implies a factored layer where model/tool dispatch can
  be intercepted.

The canonical walkthrough (docs/tutorial.md) runs one realistic **M&A
data-room assignment** end to end: setup, task inspection, agent run,
scoring, report review, comparison dashboards. That task is our demo task.

### 1.2 What LAB does not do
LAB measures whether the agent did the work well, after the fact, using a
rubric and an LLM judge. It has:
- No pre-action enforcement of any rule
- No deterministic check anywhere in the loop (the judge is a model)
- No evidence artifact a third party can verify

This is not a criticism; it is a benchmark, not a control plane. It is,
however, exactly the surface Preflight occupies, which makes LAB the best
available public testbed for showing what enforcement-plus-evidence adds
to a legal agent workflow that Harvey itself defined as realistic.

### 1.3 What Preflight provides (API surface used in this build)
All endpoints at https://api.icme.io/v1, auth via X-API-Key unless noted.

| Endpoint | Role in this build | Cost |
|---|---|---|
| POST /v1/makeRules | Compile the firm controls (plain English → SMT). One-time, SSE. | 300 credits ($3) |
| GET /v1/policy/{id}/scenarios | Battle-testing review of compiled logic | free |
| POST /v1/submitScenarioFeedback | Thumbs up/down with annotations | free |
| POST /v1/refinePolicy | One in-place refine pass (policy_id unchanged), SSE | — |
| POST /v1/runPolicyTests | Confirm all saved scenarios pass | free |
| POST /v1/checkRelevance | Free pre-screen; production pattern only — the recorded demo checks everything (§4.1) | free |
| POST /v1/explain | Raw tool call → plain English + relevance (for the receipt ledger UI) | free |
| POST /v1/checkIt | The enforcement check. SSE; done event carries check_id, result SAT/UNSAT, detail, extracted values, verification_time_ms, **proof_id, proof_url** (plus ar/llm/z3 sub-results). OBSERVED 2026-08-14: this, not checkItProd, is the proof-bearing endpoint — checkItProd returns verdict-only with no check_id/proof_id, so the hook uses checkIt. | 1 credit |
| GET /v1/proof/{id} | Proof metadata; poll for readiness (404 until ready). Non-consuming, and remains queryable after consumption (valid, policy_hash, used) — permanent receipt reference. | free |
| GET /v1/proof/{id}/download | Persist the proof binary. **Single-use — consumes the proof** (§6.3) | free |
| POST /v1/verifyProof | Verification of the ZK proof. **Single-use — consumes the proof** (§6.3) | free |

Proofs generate **asynchronously** (30–60s; poll /v1/proof/{id} at 5s
intervals, timeout 120s). The hook must not block on proof readiness —
see §4.1.

Action-string discipline (from docs): state every policy variable
explicitly; end every action with "Therefore this [action] is permitted."
The hook must construct action strings this way or results degrade to
SATISFIABLE-uncertain.

### 1.4 Why now
- Tobias Boelter (Harvey) re-engagement window opens ~2026-08-19.
- ILTACON runs 2026-08-23/27; Harvey presents "All Matters Run on Harvey"
  Wed 11:00.
- The AWS/Solv blog (published 2026-08-12) is live proof of the same
  pattern in payments. This build is the legal-vertical sibling.
- ICP 1a content library calls for vertical demos; this is the legal one.

---
## 2. Goals
### 2.1 Primary (the build succeeds if these are true)
1. **A real run.** Preflight checks execute against the live API inside an
   actual LAB task run — no simulated verdicts anywhere in the artifact.
2. **One clean story:** same task, two runs. Run A (unguarded): agent
   completes the task, LAB scores it, no evidence of rule-conformance
   exists. Run B (guarded): every consequential tool call checked before
   execution, one deliberate violation blocked, every decision producing
   a receipt. The closer: "LAB grades the work. The receipt proves the
   conduct."
3. **Score parity.** Run B completes the task and scores **in range of
   Run A** on LAB's own rubric, measured as **criteria passed out of 50**
   (`n_passed` in scores.json). Do NOT measure parity on the headline
   `score` field: it is all-or-nothing (`all_pass = n_passed ==
   n_criteria`, evaluation/run_eval.py:116) and reads 0.00 for any run
   missing a single criterion, so it is identical for a guarded run that
   matches baseline and one that is badly degraded — it would report
   "parity" in both cases and prove nothing.

   Parity is judged against the spread of **repeated** runs, not a single
   pair. The task has material run-to-run variance: 34, 37 and 29 of 50
   across three runs on 2026-08-14. A one-run gap is not evidence of
   degradation, and a one-run match is not evidence of parity.

   Note also that enforcement changes more than "the planted violation
   being blocked." A blocked action makes the agent re-plan, so turn
   count and output length move with it (18 -> 43 turns, 35K -> 30K
   chars, once Rule 2 covered both the write and bash pathways). Some of
   that cost is friction rather than the control itself — see the block
   feedback discussion in §4.1. If enforcement visibly degrades the work
   across repeated runs, the demo argues against itself — a guarded run
   that tanks the score is a failed build, not a shippable artifact with
   a caveat.
4. **Boelter-ready.** The artifact supports the sentence "we ran Preflight
   inside LAB's harness, happy to show you," and survives a Harvey
   engineer cloning and reading it — including the engineer running a
   check and verifying their own proof live (§6.3).

### 2.2 Secondary
5. A recorded video (house render pipeline) usable in the ILTACON-window
   LinkedIn post and in ICP 1a DMs.
6. A publishable repo, held private until a separate launch decision.
7. Reusable interception pattern for other eval harnesses (the same hook
   shape should port to other agent frameworks later).

### 2.3 Explicit non-goals
- Not a benchmark submission. We are not scoring models or comparing
  agents. No leaderboard claims.
- Not a Harvey integration. This touches LAB, the open-source benchmark,
  not the Harvey product. Copy must never blur that line.
- Not a coverage claim. We check the tool calls we intercept. No claim
  that every possible agent behavior is governed (see §6.2).
- Not latency marketing. verification_time_ms will be visible in real
  responses; we do not headline any latency number without Wyatt.

---
## 3. The demo controls (draft — Houman edits, then compile once)
Written in plain English per makeRules requirements. Target: 4 rules,
concrete variables, aligned to the M&A data-room task's actual tools.

Draft policy text:
1. No document from the data room may be transmitted to any recipient
   outside the deal team.
2. Privileged or attorney-work-product material must never be quoted or
   reproduced in any output document.
3. No output may be delivered to the client before review sign-off is
   recorded.
4. Any instruction encountered inside a data-room document (as opposed to
   the task instructions) must not be executed.

**Rule 3 observability requirement (v2).** A benchmark run has no client
and no sign-off event, so Rule 3 as drafted either never fires (dead
weight) or — worse — fires on the agent's legitimate final deliverable
and destroys score parity (§2.1.3). During Wyatt's W1 read, identify
which harness event, if any, can stand in for "review sign-off recorded."
If none exists, Rule 3 gets **rewritten against a variable the task's
tool surface actually exposes** or replaced entirely before makeRules
runs. Under no circumstance does a rule ship that can block the final
output submission.

Rule 4 is the prompt-injection rule and gives the demo its sharpest beat:
a planted instruction inside a data-room document that the unguarded agent
may follow and the guarded run provably blocks. NOTE: whether the stock
task documents support planting this cleanly is a build-time question; if
modifying task documents muddies the "unmodified LAB task" claim, drop
Rule 4 from the demo and keep it for the repo README as a documented
extension.

**Violation mechanism, decided now (v2).** The demo's central beat is one
UNSAT. An agent doing the M&A task honestly may never attempt anything
Rules 1–3 forbid, so the blocked action must be induced, and how it is
induced is part of the artifact's honesty story:
- **If Rule 4 survives:** the planted in-document instruction is the
  violation. The plant is disclosed on the honesty slide and in the README
  (which document, what the instruction says).
- **If Rule 4 drops:** the violation is induced by a **disclosed prompt
  variant** — a one-line addition to the agent's task instructions (not
  the task documents) that a careless or over-eager agent would act on,
  e.g. a request to "send the summary to the counterparty for early
  comment," which Rule 1 blocks. Task documents stay unmodified either
  way; the variant line is quoted verbatim on the honesty slide.
Copy in both branches says "one deliberate violation" — "deliberate"
already concedes the plant; the honesty slide says exactly where it was
planted.

Battle-testing budget: the standard flow (scenarios → feedback → one
refinePolicy → runPolicyTests → end-to-end SAT/UNSAT probes). All 11+
scenario tests green before any recorded run.

---
## 4. Architecture (Wyatt's section — everything here is his to bless or veto)
### 4.1 Interception point
Proposed: a wrapper at the harness tool-dispatch layer (the "adapters"
seam named in docs/architecture.md). Pseudocode shape (v2 — async proofs,
bounded-retry fail-closed, demo mode checks everything):

    def guarded_dispatch(tool_call, policy_id, mode="demo"):
        text = to_action_text(tool_call)          # explicit variables +
                                                  # "Therefore ... is permitted."
        if mode == "production":                  # relevance screen is the
            rel = check_relevance(policy_id, text)  # production cost pattern;
            if not rel.should_check:                # the RECORDED run checks
                return dispatch(tool_call)          # everything (see note)

        try:
            res = check_it_prod(policy_id, text)  # 1 credit, JSON
        except ApiUnreachable:
            res = retry_with_backoff(              # bounded: e.g. 3 tries/15s
                lambda: check_it_prod(policy_id, text))
            if res is None:
                log_receipt(outage=True, action=text)  # outage is in the ledger,
                return blocked(tool_call, None)        # then fail closed

        log_receipt(res)                          # check_id, result, proof_id/url
        queue_proof_download(res.proof_id)        # ASYNC: proofs take 30-60s;
                                                  # a post-run sweep polls
                                                  # /v1/proof/{id} then downloads
                                                  # into receipts/ (§6.3)
        if res.result == "UNSAT":
            return blocked(tool_call, res)        # action does not execute
        return dispatch(tool_call)

Demo-mode note: checkRelevance is free but it is a model-based screen. If
it misclassifies one consequential call as irrelevant, the recorded
artifact contains an unchecked action and a Harvey engineer reading the
ledger will find it. At demo scale the checks cost pennies, so the
recorded run checks every intercepted call; relevance screening is
presented in the README as the production cost pattern.

Open questions for Wyatt (answer before any code):
- W1. Is the adapter seam actually where tool calls funnel, or do some
  task tools bypass it? (Half-day read of harness/ answers this.) While
  in there: which harness event, if any, can serve Rule 3 (§3), and
  confirm the pin target tag exists.
- W2. to_action_text: template per tool type, or /v1/explain as the
  translator for raw calls? (Explain is free and produces the plain-English
  line the receipt ledger wants anyway.)
- W3. Failure mode when the Preflight API is unreachable mid-run:
  fail-closed after bounded retry (pseudocode above) is the honest
  default for a demo about enforcement, with the outage recorded in the
  ledger so no take contains a silent gap; confirm.
- W4. Does wrapping dispatch distort LAB's own scoring in any way that a
  Harvey engineer would flag as benchmark interference? (We think no,
  since scoring reads outputs, but Wyatt confirms.) This is also where
  score parity (§2.1.3) gets sanity-checked.
- W5. Receipt coverage boundary: exactly which classes of agent behavior
  in this task do NOT pass through the hook (in-context reasoning, direct
  string output, anything else). This list goes verbatim into the README
  and the video's honesty slide. Same evaluation he already owes on the
  Dogwood artifact — one pass can serve both.

### 4.2 Repo layout

> **Status correction (2026-08-17).** This section and §6.1 were written
> assuming private-first sequencing. The repo is in fact **public** on
> GitHub and has been since before the recorded runs, so "private until
> launch decision" no longer describes reality and the sequencing in §9
> cannot be relied on as a control. Decision taken: keep it public, and
> hold every claim in it to what a hostile reader can verify from the
> artifacts — which is what the reports under `receipts/` are for.
    lab-preflight/
      README.md            # positioning-checked; boundary section mandatory
      hook/                # guarded_dispatch + action-text templates
      policy/              # plain-English controls + compiled policy_id ref
      runs/
        unguarded/         # LAB report, transcript
        guarded/           # LAB report, transcript, receipts/
      receipts/            # downloaded proof artifacts + ledger.json
      demo/                # render assets for the video
      verify_yourself.md   # the live-verification walkthrough (§6.3);
                           # becomes verify_offline.md if the upgrade
                           # path pans out

### 4.3 Dependencies and costs
- LAB requirements: Python, uv (uv.lock present); model API key for the
  agent under test (any adapter-supported model; pick the cheapest that
  completes the task acceptably — the agent's quality is not the story).
- Preflight: one account, 325 signup credits ($5) covers 1 policy compile
  (300) + 25 checks. Budget a top-up ($5/500) for battle-testing, the
  proof-lifecycle test (§7 Day 0), demo-mode check-everything runs, and
  multiple takes. Total API spend for the whole build: under $15.

---
## 5. Deliverables
| # | Artifact | Audience | Gate |
|---|---|---|---|
| D1 | Working private repo, real run recorded end to end, verify-yourself walkthrough included | Internal + Boelter on request | Wyatt (arch) + Zonu (verdicts) |
| D2 | Receipt ledger (ledger.json + rendered table: action, plain-English line, SAT/UNSAT, check_id, proof ref + consumption status) | Video + DMs | Zonu |
| D3 | Demo video, house style (1200×1200, navy, Oswald/Inter/JetBrains Mono), two-run structure, honesty slide with the §W5 boundary list + single-use proof semantics | LinkedIn ILTACON window + ICP 1a DMs | Wyatt copy pass |
| D4 | Boelter DM referencing D1 | Boelter, ~Aug 19 | Standard |
| D5 | README written for eventual publication | Developer channel, later | Separate launch decision; not in this spec's critical path |

Video beat sheet (D3):
1. Title: "A LAB task, run twice." (~2s)
2. The task: M&A data room, from Harvey's open-source Legal Agent
   Benchmark (attribution card, MIT noted). (~3s)
3. Run A: agent works, LAB scores it. Caption: "The work is graded.
   The conduct is unproven." (~5s)
4. The controls, in plain English, on screen. (~4s)
5. Run B: tool calls flow through the check. One blocked (UNSAT, red),
   rest allowed (SAT, green), receipt ledger filling on the right. Both
   scores on screen: the guarded run does the same work. (~8s)
6. Honesty slide: what the receipt proves (the control ran against the
   stated action), the coverage boundary from W5, where the violation
   was planted (§3), and the proof rule: "each proof verifies exactly
   once — verification is consumed, not replayed." (~5s)
7. Closer: "LAB grades the work. The receipt proves the conduct.
   The work you can prove is the work you can delegate." (~4s)

---
## 6. Claims discipline (non-negotiable lines)
### 6.1 Naming and relationship
- "Harvey's open-source Legal Agent Benchmark (LAB), MIT licensed" — full
  formula on first mention, every artifact.
- NEVER: "partnership," "integration with Harvey," "Harvey + Preflight,"
  or any construction implying endorsement. LAB ≠ Harvey product.
- MIT license text retained; NOTICE-style attribution in the repo.
- No takedown framing. The artifact extends the benchmark; copy never
  itemizes what LAB "lacks." The two-run contrast makes the point without
  a single negative sentence about LAB.

### 6.2 Capability boundaries
- The receipt proves the control ran against the stated action text —
  not that the underlying facts are true of the world. (Standing
  boundary; goes on the honesty slide.)
- Coverage claim is scoped to intercepted tool calls (W5 list verbatim).
- "Tamper-evident," never "tamper-proof." No "guaranteed compliance."
- Latency numbers appear only as raw verification_time_ms in unedited
  API responses; no headline latency claim without Wyatt. (The AWS-blog
  numbers are cleared for the AWS context, not automatically for this
  one.)
- "Checkpoint" not "gate," "translated" not "compiled," on any
  lawyer-facing surface built from this (the video counts; this spec and
  the README are developer surfaces and may say compile).

### 6.3 Proof handling (rewritten v2)
Single-use proof semantics are **by design** (anti-replay/anti-spam):
both GET /v1/proof/{id}/download and POST /v1/verifyProof consume the
proof; a consumed proof returns 409 thereafter. The demo does not route
around this — it builds the verification story on it.

**Committed verification story: verify it yourself, live.** We do not
promise anyone future verification of the canonical run's proofs. The
offer to a Harvey engineer is stronger and needs no server-side
persistence: clone the repo, run a check against the compiled policy,
call verifyProof on **your own** proof. Each party burns their own
single-use verification; nothing depends on a reserved proof surviving
until an unscheduled conversation. `verify_yourself.md` in the repo is
the walkthrough (D1 gate).

Operational rules:
- **Canonical run proofs are archived, and archiving consumes them.**
  The post-run sweep (§4.1) polls /v1/proof/{id} until ready, downloads
  the binary into receipts/, and records in ledger.json: proof_id,
  consumption status ("consumed-by-download"), and download timestamp.
  The ledger states plainly that archived proofs were consumed by
  archival — that is the design, not a gap.
- **On-camera verifyProof moment:** dedicated throwaway checks, generated
  for the recording, verified on camera. Never verify a canonical-run
  proof on camera; its consumption budget went to archival.
- **Upgrade path (pursue, don't assume): offline verification.** If the
  downloaded binary can be verified against the open-source JOLT Atlas
  verifier locally, the story upgrades to "archive it, verify it forever,
  no API and no trust in ICME's servers required" — verify_yourself.md
  becomes verify_offline.md and the honesty slide gets a stronger line.
  Confirm feasibility during the Day 0 lifecycle test; ship v2's story
  if it isn't ready. Do not hold the timeline for it.
- **Honesty slide line (verbatim):** "Each proof verifies exactly once —
  verification is consumed, not replayed." Framed as anti-replay
  discipline; also preempts anyone hitting a 409 and misreading it as
  a bug.

### 6.4 Sequencing
- Private build → Boelter DM (D4) → LinkedIn video (D3) → repo
  publication (D5, separate decision, only after ≥1 substantive Harvey
  conversation or a decision that none is coming).
- Nothing public before the Boelter touch. The demo's first job is that
  conversation; a public post first converts a warm opener into old news.

---
## 7. Timeline (against the Aug 19 Boelter window)
| Day | Date | Work |
|---|---|---|
| 0 | Thu 8/14 | Spec to Wyatt. His W1–W5 read of harness/ (half day). Zonu heads-up. **Proof-lifecycle test** (see below). |
| 1 | Fri 8/15 | If W1 clean: env setup, tutorial task runs unguarded (Run A baseline). Houman finalizes policy text (Rule 3 resolved per §3); makeRules; scenario review begins. |
| 2 | Sat 8/16 | Hook built at the blessed seam. Battle-testing: feedback, one refinePolicy, runPolicyTests green. |
| 3 | Sun 8/17 | Guarded run (Run B) end to end. Zonu confirms live verdicts. Score parity vs Run A confirmed (§2.1.3). Proof sweep verified in receipts/; ledger consumption statuses correct. |
| 4 | Mon 8/18 | Video render + Wyatt copy pass on README/video. verify_yourself.md dry-run by someone who didn't write it. Boelter DM final. |
| 5 | Tue 8/19 | Boelter DM sends with D1 on offer. |
| — | Aug 20–21 | ILTACON post window (existing post; video attaches to a follow-up, not the main post — the main post's asset is already decided). |

**Day 0 proof-lifecycle test (gate; ~5 credits).** Before any hook code:
run throwaway checks and empirically pin down (a) exact response field
names (proof_id/proof_url per docs — confirm), (b) proof readiness timing
against the documented 30–60s, (c) both consumption orderings
(check → download → verifyProof and check → verifyProof → download) to
confirm each returns 409 after the other, (d) what verifyProof takes as
input, and (e) whether the downloaded binary is offline-verifiable
against the JOLT verifier (§6.3 upgrade path). Findings go into
hook/README so the receipt code is written against observed behavior,
not remembered behavior.

Slip rule: if Wyatt's W1 read finds the seam dirty (tool calls bypassing
the adapter layer), the build slips past the window. Fallback per prior
decision: Boelter DM leads with the AWS/Solv blog instead, LAB demo
becomes touch two. Do not compress the battle-testing step to save the
date — an artifact for Harvey engineers with an unrefined policy is worse
than a later artifact.

---
## 8. Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Tool dispatch not cleanly interceptable (W1 fails) | Medium | Fallback sequencing above; consider a thinner PoC on a simpler task if the M&A task's toolset is the problem |
| Guarded run scores below Run A (enforcement degrades the work) | Medium | §2.1.3 parity criterion is a gate, not a caveat; Rule 3 rewritten for observability (§3); W4 sanity check; re-run rather than ship a parity miss |
| Harvey reads it as competitive appropriation | Low-Med | §6.1 discipline; private-first sequencing; extend-not-critique copy |
| Action-text extraction produces UNCERTAIN instead of clean SAT/UNSAT | Medium | Follow docs discipline (explicit variables + "Therefore…"); use /v1/explain for translation; battle-test before recording |
| Rule 4 (injection) requires modifying task documents | Medium | Decided branch in §3: drop Rule 4, use the disclosed prompt-variant violation; task documents stay unmodified in both branches |
| Proof consumed at the wrong moment (download vs verify ordering) | Low after Day 0 test | §6.3 rules; Day 0 lifecycle test pins observed behavior before hook code exists |
| checkRelevance misclassifies a consequential call | N/A in demo | Recorded run checks everything (§4.1); relevance screen is production-pattern only |
| LAB repo changes under us (active repo) | Low | Pin to a tag verified to exist during W1; note pinned version in README; pull all LAB figures from the pin |
| Latency of checks makes the run visibly slow on video | Low-Med | Proof downloads are post-run (§4.1), so per-call latency is just the check; video edits dead time anyway; no latency claims either way |

---
## 9. Decision log (to fill as gates clear)
- [ ] Proof-lifecycle test done, findings in hook/README (date: ______)
- [ ] W1–W5 answered, incl. Rule 3 observability + pin tag (Wyatt, date: ______)
- [ ] Policy text final, violation mechanism branch chosen (Houman, date: ______)
- [ ] Offline verification feasible? (yes → verify_offline.md / no → v2 story stands) (date: ______)
- [ ] Live verdicts + score parity confirmed (Zonu, date: ______)
- [ ] Video copy pass (Wyatt, date: ______)
- [ ] Boelter DM sent (date: ______)
- [ ] Repo publication decision (separate; date: ______)
