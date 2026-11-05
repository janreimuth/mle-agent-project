"""Scripted stand-in for the Groq model, for testing the evaluation without tokens.

OracleClient plays each case the way the case expects: it calls the expected tools,
searches for the expected sources, drafts with the expected status and cites the
expected sources. Every case should pass against it. A case that fails against the
oracle is impossible to pass as written (or the evaluator is wrong).

A fault turns the oracle into a specific bad agent, to check that the evaluator
catches that mistake. FAULTS maps each fault to the failure category it must cause.

The client speaks the same API shape as Groq's chat.completions.create, so the agent
loop, the tools, the guardrails and the evaluator all run for real; only the model's
decisions are scripted.
"""
from __future__ import annotations

import json
import re
from itertools import count
from types import SimpleNamespace as NS

from retrieval import PolicyIndex, terms
from validate_data import AgentCase, Dataset

UPDATE_STATUSES = {"answered", "needs_info", "escalated", "refused"}

# fault -> failure categories (from run_evaluation.classify) that must be reported
FAULTS = {
    "skip_tool": {"tool selection"},
    "wrong_status": {"wrong status"},
    "fake_citation": {"grounding", "stopped at limit"},
    "loop_until_limit": {"stopped at limit"},
}

_ids = count()


def _call(name: str, args: dict) -> NS:
    return NS(id=f"mock_{next(_ids)}", type="function",
              function=NS(name=name, arguments=json.dumps(args)))


def _query_for(source_id: str, index: PolicyIndex) -> str:
    """A query that retrieves the source: its title words, else its ID words."""
    title = next((p.title for p in index.passages if p.source_id == source_id), source_id)
    # Plain words, not stems: the tool stems the query itself, like for a real agent.
    words = [w for w in re.findall(r"[A-Za-z0-9-]+", title) if terms(w)]
    if len(words) < 2:
        words = source_id.split("-") + ["policy"]
    return " ".join(words[:5])


def build_plan(case: AgentCase, data: Dataset, index: PolicyIndex,
               fault: str | None = None) -> list:
    """List of model replies: each a list of tool calls, or a final text."""
    ticket = next((t for t in data.tickets if t.ticket_id == case.ticket_id), None)
    team = ticket.category if ticket else "support"
    cites = " ".join(f"[{s}]" for s in case.expected_sources)
    status = case.expected_status
    if fault == "wrong_status":
        status = {"answered": "needs_info", "needs_info": "answered"}.get(status, "needs_info")

    steps: list = []
    for tool in case.expected_tools:
        if fault == "skip_tool" and tool == _skippable(case):
            continue
        tid = {"ticket_id": case.ticket_id} if case.ticket_id else {}
        if tool in ("get_ticket", "get_customer_context") and tid:
            steps.append([_call(tool, tid)])
        elif tool == "search_policy":
            text = ticket.subject if ticket else (case.prompt or "help request")
            plain = " ".join([w for w in re.findall(r"[A-Za-z0-9-]+", text) if terms(w)][:4])
            queries = [_query_for(s, index) for s in case.expected_sources] or [plain]
            steps.append([_call("search_policy", {"query": q}) for q in queries])
        elif tool == "check_availability":
            steps.append([_call("check_availability", {"team": team})])
        elif tool == "draft_ticket_update" and case.ticket_id:
            draft_status = status if status in UPDATE_STATUSES else "needs_info"
            msg = (f"Update on your request {cites}." if draft_status == "answered"
                   else f"We need to look into this further {cites}.").replace(" .", ".")
            steps.append([_call("draft_ticket_update", {
                "ticket_id": case.ticket_id, "status": draft_status, "message": msg})])
        elif tool == "draft_escalation" and case.ticket_id:
            if fault == "wrong_status":
                steps.append([_call("draft_ticket_update", {
                    "ticket_id": case.ticket_id, "status": "needs_info",
                    "message": "Could you send more details?"})])
            else:
                steps.append([_call("draft_escalation", {
                    "ticket_id": case.ticket_id, "team": team,
                    "reason": f"Needs a human per policy {cites}".strip(),
                    "summary": (ticket.subject if ticket else case.prompt or "Request")[:200]})])

    if fault == "loop_until_limit":
        steps = steps[:1] + [[_call("search_policy", {"query": "loop query again"})]] * 20

    if status == "abstained" and fault != "wrong_status":
        final = "No approved policy covers this request, so I can't answer it."
    elif status == "abstained":
        # The realistic failure: answering an unsupported question in plain text.
        final = "Sure! Next week the menu has pasta on Monday and soup on Friday."
    else:
        final = f"Done. {cites}".strip()
    if fault == "fake_citation":
        final = "Done, per [made-up-policy]."
    if status == "review_required" and fault != "wrong_status":
        final += "\nOUTCOME: review_required"
    steps.append(final)
    return steps


def _skippable(case: AgentCase) -> str | None:
    """The tool the skip_tool fault drops: customer context if expected, else the first."""
    tools = case.expected_tools
    return "get_customer_context" if "get_customer_context" in tools else (tools[0] if tools else None)


class PlanClient:
    """Returns the plan's next reply. Position = assistant replies since the first user turn."""

    def __init__(self, plan: list):
        self.plan = plan
        self.chat = NS(completions=self)

    def create(self, messages, **_):
        done = sum(1 for m in messages if m["role"] == "assistant")
        step = self.plan[min(done, len(self.plan) - 1)]
        msg = (NS(content=step, tool_calls=None) if isinstance(step, str)
               else NS(content=None, tool_calls=step))
        return NS(choices=[NS(message=msg)])


def mock_client_factory(data: Dataset, index: PolicyIndex, fault: str | None = None):
    return lambda case: PlanClient(build_plan(case, data, index, fault))

