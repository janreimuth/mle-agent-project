"""Compare an agent run's trace with what the evaluation case expects."""
from __future__ import annotations

from pydantic import BaseModel

from retrieval import citation_problems, extract_citations
from tools import Trace
from validate_data import AgentCase

DRAFT_TOOLS = ("draft_ticket_update", "draft_escalation")
READ_TOOLS = ("get_ticket", "get_customer_context", "search_policy", "check_availability")
# Statuses that mean "did not answer": abstain, ask, refuse or hand off.
ABSTAIN = {"needs_info", "refused", "escalated", "abstained", "review_required"}


class ToolEvaluation(BaseModel):
    case_id: str
    expected_tools: list[str]
    used_tools: list[str]
    missing_tools: list[str]       # expected but never called successfully
    unexpected_tools: list[str]    # called but not expected for this task
    failed_calls: list[str]        # "step 3 search_policy: <error>"
    order_ok: bool                 # expected read/search tools ran before the first draft
    expected_sources: list[str]
    retrieved_sources: list[str]   # accepted evidence returned by search_policy
    cited_sources: list[str]       # valid citations in drafts or the final answer
    missing_sources: list[str]     # expected but not cited
    citation_errors: list[str]     # citations outside accepted evidence, uncited answer
    supported: bool                # any search returned accepted evidence
    abstained: bool                # did not answer: abstained, asked, refused or handed off
    expected_status: str | None
    final_status: str | None       # last draft's status, "review_required", or None (no draft)
    status_ok: bool
    memory_used: bool              # get_customer_context returned memory entries
    final_outcome: str | None      # completed / escalated / review_required (agent loop)
    stopped_by_limit: list[str]    # reasons of turns stopped by a limit or an unusable answer
    passed: bool


def status_matches(expected: str | None, final_status: str | None, outcome: str | None) -> bool:
    if expected is None:
        return True
    if expected == "review_required":
        return outcome == "review_required"
    if expected == "abstained":
        # No answer given: no draft at all, or a draft that asks or refuses.
        return outcome == "completed" and final_status in (None, "needs_info", "refused")
    return final_status == expected


def dependency_order_ok(expected: list[str], trace: Trace) -> bool:
    """Every expected read/search tool that was used ran before the first draft.
    The order among read tools is free: searching policy before reading the ticket is fine."""
    calls = [c.tool for c in trace.calls if c.ok]
    drafts = [i for i, t in enumerate(calls) if t in DRAFT_TOOLS]
    if not drafts:
        return True
    first_draft = drafts[0]
    for tool in expected:
        if tool in READ_TOOLS and tool in calls and calls.index(tool) > first_draft:
            return False
    return True


def evaluate(case: AgentCase, trace: Trace, final_answer: str | None = None) -> ToolEvaluation:
    """final_answer: the agent's last text reply, if any; its citations are checked too."""
    used = trace.tools_used()
    succeeded = [c.tool for c in trace.calls if c.ok]
    expected = case.expected_tools
    final_outcome = trace.outcomes[-1].outcome if trace.outcomes else None

    retrieved = {e["source_id"] for o in trace.outputs("search_policy") for e in o["evidence"]}
    cited = {s for t in DRAFT_TOOLS for o in trace.outputs(t) for s in o["cited_sources"]}
    updates = trace.outputs("draft_ticket_update")
    final_status = updates[-1]["status"] if updates else None
    if final_status is None and trace.outputs("draft_escalation"):
        final_status = "escalated"
    if final_status is None and final_outcome == "review_required":
        final_status = "review_required"

    citation_errors = [f"cited [{s}] without accepted evidence" for s in sorted(cited - retrieved)]
    if final_answer is not None:
        # A citation is only required when the agent answers; questions and hand-offs need none.
        citation_errors += citation_problems(
            final_answer, retrieved, require_citation=final_status == "answered")
        cited |= {s for s in extract_citations(final_answer) if s in retrieved}
    # An answer without supporting evidence is the "nearest weak match" failure.
    if final_status == "answered" and not retrieved:
        citation_errors.append("answered although no evidence passed the threshold")

    stopped = [o.reason or "stopped" for o in trace.outcomes if o.stopped]
    memory_used = any(o["memory"] for o in trace.outputs("get_customer_context"))
    missing_tools = [t for t in expected if t not in succeeded]
    unexpected = sorted(set(used) - set(expected)) if expected else []
    failed = [f"step {c.step} {c.tool}: {c.error}" for c in trace.calls if not c.ok]
    order_ok = dependency_order_ok(expected, trace)
    missing_sources = [s for s in case.expected_sources if s not in cited]
    status_ok = status_matches(case.expected_status, final_status, final_outcome)
    memory_ok = memory_used or not case.must_use_memory
    abstained = final_status in ABSTAIN or (final_status is None and final_outcome == "completed"
                                            and not cited)

    return ToolEvaluation(
        case_id=case.case_id, expected_tools=expected, used_tools=used,
        missing_tools=missing_tools, unexpected_tools=unexpected, failed_calls=failed,
        order_ok=order_ok, expected_sources=case.expected_sources,
        retrieved_sources=sorted(retrieved), cited_sources=sorted(cited),
        missing_sources=missing_sources, citation_errors=citation_errors,
        supported=bool(retrieved), abstained=abstained, expected_status=case.expected_status,
        final_status=final_status, status_ok=status_ok, memory_used=memory_used,
        final_outcome=final_outcome, stopped_by_limit=stopped,
        passed=not (missing_tools or unexpected or failed or missing_sources or citation_errors
                    or stopped)
        and order_ok and status_ok and memory_ok,
    )
