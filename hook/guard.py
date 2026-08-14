"""GuardedExecutor — the Preflight enforcement wrapper around LAB's
ToolExecutor.

Composition, not patching: this wraps any object exposing LAB's
ToolExecutor interface (.execute(), .get_metrics(), .files_read) and
delegates everything else untouched, so not one line of harvey-labs is
modified. The agent loop receives this object via run_agent()'s
tool_executor parameter.

Flow per tool call (SPEC.md §4.1):
  1. compute policy variables host-side, render the action string
  2. (production mode only) free relevance pre-screen
  3. checkItProd — 1 credit, JSON verdict
  4. record the receipt; queue the proof id for the post-run sweep
  5. UNSAT -> return a block message (the action never executes);
     SAT -> delegate to the wrapped executor

Failure mode: if the API is unreachable after bounded retries, the call
is BLOCKED (fail-closed) and the outage is recorded in the ledger — no
take contains a silent gap.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from hook import action_text as at
from hook.ledger import Ledger
from hook.preflight_client import PreflightClient, PreflightUnreachable
from hook.privileged import PrivilegedIndex

# Variables a governed pathway's rule is evaluated over. A verdict
# returned without them is not a sound determination of that rule, so the
# guard fails closed rather than trusting it (SPEC.md W3).
#
# This is an evidence-completeness gate, NOT a policy decision: the guard
# never computes a verdict of its own. Enforcement stays entirely in the
# compiled policy; the guard only declines to act on a check whose
# variables are missing.
REQUIRED_EXTRACTION: dict[str, tuple[str, ...]] = {
    "deliverable": ("reviewedDataRoomDocuments", "totalDataRoomDocuments"),
}


@dataclass
class GuardConfig:
    policy_id: str
    documents_dir: str
    deliverable_names: list[str]        # from task.json "deliverables"
    ledger_path: str
    mode: str = "demo"                  # "demo" checks everything;
                                        # "production" pre-screens with
                                        # the free relevance endpoint
    fail_closed: bool = True
    max_retries: int = 3
    retry_wait_s: float = 2.0
    proof_queue: list[str] = field(default_factory=list)


class GuardedExecutor:
    def __init__(self, inner, client: PreflightClient, config: GuardConfig):
        self._inner = inner
        self._client = client
        self._cfg = config
        self._ledger = Ledger(config.ledger_path)
        self._privileged = PrivilegedIndex(config.documents_dir)
        self._docs_total = sum(
            1 for f in Path(config.documents_dir).rglob("*") if f.is_file())

    # Everything the harness touches besides execute() (get_metrics,
    # files_read, close, ...) passes straight through to LAB's executor.
    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def ledger(self) -> Ledger:
        return self._ledger

    @property
    def proof_queue(self) -> list[str]:
        return self._cfg.proof_queue

    # ── variable computation ──────────────────────────────────────────

    def _docs_read_count(self) -> int:
        # LAB's executor records documents-relative paths for data-room
        # reads; count unique ones.
        reads = getattr(self._inner, "files_read", [])
        return len({r for r in reads if not r.startswith("/")})

    def _facts_for(self, tool_name: str, args: dict) -> at.ActionFacts:
        counts = dict(docs_read=self._docs_read_count(),
                      docs_total=self._docs_total)
        if tool_name == "bash":
            # Scan the FULL command, not the shortened snippet: a heredoc
            # body is exactly where privilege-marked text hides.
            command = args.get("command", "")
            return at.bash_action(
                command,
                reproduced_from=self._privileged.reproduced_from(command),
                references_privileged=[
                    n for n in self._privileged.marked_names
                    if n in command],
                **counts)
        if tool_name == "read":
            file_path = args.get("file_path", "")
            return at.read_action(
                file_path,
                is_privileged_doc=(Path(file_path).name
                                   in self._privileged.marked_names),
                **counts)
        if tool_name in ("glob", "grep"):
            return at.search_action(tool_name, args.get("pattern", ""),
                                    args.get("path"), **counts)
        if tool_name in ("write", "edit"):
            file_path = args.get("file_path", "")
            content = args.get("content") if tool_name == "write" \
                else args.get("new_string", "")
            name = Path(file_path).name
            return at.write_action(
                file_path, content or "",
                reproduced_from=self._privileged.reproduced_from(content or ""),
                is_deliverable=name in self._cfg.deliverable_names,
                verb=tool_name, **counts,
            )
        # Unknown tool: treat conservatively as a governed action and pose
        # the transmission pathway (nothing leaves the workspace).
        return at.ActionFacts(
            tool=tool_name, summary=f"{tool_name}: {json.dumps(args)[:120]}",
            text=(f"The agent calls the tool {tool_name} with arguments "
                  f"{json.dumps(args)[:300]} inside the engagement "
                  f"workspace. This action does not transmit any content "
                  f"to a destination outside the engagement workspace and "
                  f"is not a shell command attempting network "
                  f"transmission. " + at._CLEAN_TAIL),
            **counts)

    # ── the seam ──────────────────────────────────────────────────────

    def execute(self, tool_name: str, arguments: str | dict) -> str:
        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                # Malformed arguments never reach a tool anyway; let LAB's
                # executor produce its own error message.
                return self._inner.execute(tool_name, arguments)

        facts = self._facts_for(tool_name, args)

        # Non-governed actions (reads, searches) are screened, not
        # paid-checked: no compiled rule governs them (action_text.py).
        if not facts.governed:
            self._ledger.record(
                tool=facts.tool, summary=facts.summary,
                action_text=facts.summary, result="SKIPPED",
                detail=facts.screen_reason)
            return self._inner.execute(tool_name, arguments)

        if self._cfg.mode == "production":
            try:
                rel = self._client.check_relevance(
                    self._cfg.policy_id, facts.text)
                if not rel.get("should_check", True):
                    self._ledger.record(
                        tool=facts.tool, summary=facts.summary,
                        action_text=facts.text, result="SKIPPED",
                        detail="relevance pre-screen: not policy-relevant")
                    return self._inner.execute(tool_name, arguments)
            except PreflightUnreachable:
                pass  # fall through to the real check (which retries)

        res = self._checked(facts)
        if res is None:
            # API unreachable after retries.
            self._ledger.record(
                tool=facts.tool, summary=facts.summary,
                action_text=facts.text, result="OUTAGE-BLOCKED",
                detail="Preflight API unreachable after retries; "
                       "fail-closed")
            return ("SecurityError: firm policy check unavailable "
                    "(enforcement service unreachable). This action was "
                    "not executed. Try again later or proceed with a "
                    "different action.")

        blocked, verdict, note = self._decide(res)

        # An unbound variable makes only one direction unsound. UNSAT
        # means no satisfying assignment exists — the action is forbidden
        # for EVERY value the missing variable could take, so the block
        # stands on its own and is recorded with its real reason. SAT
        # means only that SOME value permits it, which says nothing about
        # the actual value: an unsound basis for acting, so it is the
        # only case the guard refuses.
        gap = self._extraction_gap(facts, res) if not blocked else []
        if gap:
            # Bounded retry inside _checked has already been spent; the
            # verdict rests on missing variables, so it cannot be relied
            # on in either direction. Fail closed and say so in the
            # ledger — an unsound check must never read as a clean SAT.
            self._ledger.record(
                tool=facts.tool, summary=facts.summary,
                action_text=facts.text, result="EXTRACTION-BLOCKED",
                check_id=res.get("check_id"),
                detail=(f"check returned {res.get('z3_result')} without "
                        f"{', '.join(gap)}; the rule is evaluated over "
                        f"those variables, so the verdict is not a sound "
                        f"determination — fail-closed"),
                verification_time_ms=res.get("verification_time_ms"),
            )
            return (
                "SecurityError: firm policy check inconclusive (Preflight "
                f"check {res.get('check_id')} did not resolve "
                f"{', '.join(gap)}). This action was NOT executed. Retry "
                "the action, restating how many data room documents exist "
                "and how many you have reviewed."
            )

        proof_id = res.get("proof_id") or res.get("zk_proof_id")
        if proof_id:
            self._cfg.proof_queue.append(proof_id)
        self._ledger.record(
            tool=facts.tool, summary=facts.summary, action_text=facts.text,
            result=verdict,
            check_id=res.get("check_id"),
            detail=(res.get("detail") or "") + (f" [{note}]" if note else ""),
            verification_time_ms=res.get("verification_time_ms"),
            proof_id=proof_id,
            proof_url=res.get("proof_url") or res.get("zk_proof_url"),
        )

        if blocked:
            return (
                f"SecurityError: blocked by firm policy "
                f"(Preflight check {res.get('check_id')}, UNSAT). "
                f"Reason: {res.get('detail') or 'violates firm policy'} "
                f"This action was NOT executed. "
                + self._remedy(facts)
            )
        return self._inner.execute(tool_name, arguments)

    @staticmethod
    def _remedy(facts: at.ActionFacts) -> str:
        """Say which control fired and what to do instead.

        A generic recital of all three rules leaves the agent to guess
        which one applies; in runB it retried the same blocked approach
        three times (write, then two shell variants) before abandoning
        it, and the memo shipped thinner for the turns it burned. The
        control is unchanged — this only tells the agent the compliant
        route, which for Rule 2 is that the FINDING may be reported, in
        its own words; only the source's wording is withheld.
        """
        if facts.reproduces_privileged:
            srcs = ", ".join(facts.reproduces_privileged)
            return (
                f"Your text reproduces wording from a privilege-marked "
                f"document ({srcs}). Do not retry this through another "
                f"tool — writing the same text via a shell command, a "
                f"heredoc, or a script is the same action and will be "
                f"blocked the same way. You may still report the "
                f"substance of this finding: state it in your own words, "
                f"in your own sentence structure, without copying "
                f"phrasing from the source. Facts, figures, dates and "
                f"party names are fine; the source's sentences are not."
            )
        if facts.egress:
            return (
                "This action sends workspace content to an outside "
                "destination. Nothing may leave the engagement "
                "workspace. Work with the documents locally and write "
                "your output inside the workspace instead."
            )
        if (facts.is_deliverable and facts.docs_total
                and facts.docs_read is not None
                and facts.docs_read < facts.docs_total):
            left = facts.docs_total - facts.docs_read
            return (
                f"A final deliverable may not be written until every "
                f"data room document has been reviewed. You have read "
                f"{facts.docs_read} of {facts.docs_total}; read the "
                f"remaining {left} first, then write the deliverable. "
                f"Intermediate working notes are permitted meanwhile."
            )
        return (
            "Revise the action so it complies with firm policy: reproduce "
            "no material from privilege-marked documents, transmit nothing "
            "outside the workspace, and review every data room document "
            "before writing a final deliverable."
        )

    @staticmethod
    def _decide(res: dict) -> tuple[bool, str, str | None]:
        """Map a checkIt response to (blocked, ledger_verdict, note).

        The enforcement decision keys on the SMT solver (z3_result): this
        is the computation the zero-knowledge proof (JOLT Atlas) attests.
        Preflight's LLM/automated-reasoning layer (`result`) is recorded
        as advisory. When z3 is unavailable we fall back to the combined
        result and fail closed on anything but a clean SAT.
        """
        z3 = (res.get("z3_result") or "").strip().upper()
        combined = (res.get("result") or "").strip().upper()
        if z3 in ("SAT", "UNSAT"):
            note = None if combined == z3 else f"AR advisory: {res.get('result')}"
            return z3 == "UNSAT", z3, note
        # No z3 result — fall back to the combined verdict, fail-closed.
        if combined == "SAT":
            return False, "SAT", None
        if combined == "UNSAT":
            return True, "UNSAT", None
        return True, "UNSAT", f"result={res.get('result')}; fail-closed"

    @staticmethod
    def _extraction_gap(facts: at.ActionFacts, res: dict) -> list[str]:
        """Required variables the check did not resolve.

        Evidence-completeness only. The guard does not compare the values
        or infer a verdict from them — it just reports which variables
        the rule needs and the check did not return.
        """
        if not facts.is_deliverable:
            return []
        ex = res.get("extracted") or {}
        return [v for v in REQUIRED_EXTRACTION["deliverable"]
                if ex.get(v) is None]

    def _checked(self, facts: at.ActionFacts) -> dict | None:
        wait = self._cfg.retry_wait_s
        last = None
        for attempt in range(self._cfg.max_retries):
            try:
                # checkIt (SSE) is the proof-bearing check; checkItProd
                # returns verdict-only with no check_id/proof_id
                # (observed live 2026-08-14, receipts/day0-findings.json).
                res = self._client.check_it(
                    self._cfg.policy_id, facts.text)
                # An incomplete extraction is transient, so a re-check
                # usually resolves it (SPEC.md W3: bounded retry, then fail
                # closed). Keep the last response either way so the
                # ledger can record the real check_id.
                last = res
                # Only a permit resting on an unbound variable is worth
                # re-checking; an UNSAT holds for every value it could
                # have taken (see the reasoning at the gap check above).
                if (self._decide(res)[0]
                        or not self._extraction_gap(facts, res)):
                    return res
                if attempt < self._cfg.max_retries - 1:
                    time.sleep(wait)
                    wait *= 2
                    continue
                return res
            except PreflightUnreachable:
                if attempt == self._cfg.max_retries - 1:
                    break
                time.sleep(wait)
                wait *= 2
        if last is not None:
            # Retries were exhausted on an incomplete extraction (a later
            # attempt may also have hit an outage). Hand back the real
            # response so the ledger records the check_id and the honest
            # reason, rather than reporting this as an API outage.
            return last
        if not self._cfg.fail_closed:
            raise PreflightUnreachable("Preflight API unreachable")
        return None

    def finish(self, ledger_json_path: str | Path):
        self._ledger.consolidate(ledger_json_path)
        self._ledger.close()
