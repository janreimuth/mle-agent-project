"""Agent tools with explicit input/output contracts and a per-run trace.

Every tool call goes through `Toolbox.call(name, args)`, which
  1. validates `args` against the tool's input model,
  2. runs the tool,
  3. validates the result against the tool's output model,
  4. appends a `ToolCall` (input, output or error, timing) to `toolbox.trace`.

Policy evidence and citation rules live in retrieval.py.
Tools never mutate data: the drafting tools return drafts and keep them in
`toolbox.drafts`, so the evaluation can inspect what would have been sent.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from memory import MEMORY_NOTICE, CaseMemoryEntry, CaseMemoryStore, now_utc
from retrieval import (MIN_MATCHED_TERMS, Evidence, NearMiss, PolicyIndex,
                       citation_problems, extract_citations, terms)
from validate_data import Customer, Dataset, MemoryEntry, NonEmpty, Ticket


class Contract(BaseModel):
    """Base for tool inputs: unknown arguments are rejected, not silently dropped."""
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------
class GetTicketInput(Contract):
    ticket_id: NonEmpty = Field(description="Ticket ID, e.g. T-1001")


class GetTicketOutput(BaseModel):
    ticket: Ticket
    case_history: list[CaseMemoryEntry] = Field(
        default_factory=list, description="Durable memory of earlier work on this ticket")
    memory_notice: str = MEMORY_NOTICE


class GetCustomerContextInput(Contract):
    ticket_id: NonEmpty = Field(description="Ticket whose requester to look up. "
                                "Lookup is by ticket so only that customer's data is returned.")


class GetCustomerContextOutput(BaseModel):
    customer: Customer
    verified: bool = Field(description="Customer record is marked verified")
    matches_requester: bool = Field(description="Customer record matches the ticket requester")
    memory: list[MemoryEntry] = Field(description="Seeded memory for this customer only")
    other_cases: list[CaseMemoryEntry] = Field(
        default_factory=list, description="Durable memory of this customer's other tickets")
    memory_notice: str = MEMORY_NOTICE


class SearchPolicyInput(Contract):
    query: NonEmpty = Field(description="Short keyword query for ONE policy question, "
                            "e.g. 'duplicate payment refund'")
    top_k: int = Field(3, ge=1, le=10)


class SearchPolicyOutput(BaseModel):
    supported: bool = Field(description="True if at least one passage passed the evidence threshold")
    evidence: list[Evidence] = Field(description="Accepted evidence; only these sources may be cited")
    near_misses: list[NearMiss] = Field(description="Below threshold: NOT evidence, must not be cited")
    guidance: str


class CheckAvailabilityInput(Contract):
    team: NonEmpty = Field(description="Team name, e.g. billing, security")
    at: datetime | None = Field(None, description="Point in time (ISO 8601); defaults to now")


class OnCallShift(BaseModel):
    model_config = ConfigDict(extra="allow")
    team: str
    name: str
    contact: str | None = None
    start: datetime
    end: datetime


class CheckAvailabilityOutput(BaseModel):
    team: str
    at: datetime
    available: bool
    on_call: OnCallShift | None


UpdateStatus = Literal["answered", "needs_info", "escalated", "refused"]


class DraftTicketUpdateInput(Contract):
    ticket_id: NonEmpty
    status: UpdateStatus
    message: NonEmpty = Field(description="Reply to the customer. Cite policy source IDs in square "
                              "brackets, e.g. [refunds]. 'answered' requires at least one citation, "
                              "and only sources returned as accepted evidence may be cited.")


class DraftTicketUpdateOutput(BaseModel):
    draft_id: str
    ticket_id: str
    status: UpdateStatus
    message: str
    cited_sources: list[str]
    created_at: datetime


class DraftEscalationInput(Contract):
    ticket_id: NonEmpty
    team: NonEmpty
    reason: NonEmpty = Field(description="Why a human must take over")
    summary: NonEmpty = Field(description="Handover summary; may cite accepted sources like [refunds]")
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    assignee: str | None = Field(None, description="On-call person from check_availability, if available")


class DraftEscalationOutput(BaseModel):
    draft_id: str
    ticket_id: str
    team: str
    priority: str
    assignee: str | None
    reason: str
    summary: str
    cited_sources: list[str]
    created_at: datetime


# --------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------
class ToolCall(BaseModel):
    step: int
    tool: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.error is None


class TurnOutcome(BaseModel):
    """How one user turn ended. Recorded by the agent loop, not by a tool."""
    outcome: Literal["completed", "review_required", "escalated"]
    reason: str | None = None
    steps: int                # model calls in this turn
    tool_calls: int           # tool calls the model requested and that were executed
    tool_errors: int
    answer_retries: int = 0
    stopped: bool = False     # ended by a limit or an unusable answer (not a decision)
    first_call: int           # trace step of the turn's first tool call (calls[first_call-1:])
    fallback_escalation: bool = False  # escalation drafted by the app after a limit was hit


class Trace(BaseModel):
    case_id: str | None = None
    calls: list[ToolCall] = []
    outcomes: list[TurnOutcome] = []

    def tools_used(self) -> list[str]:
        return [c.tool for c in self.calls]

    def outputs(self, tool: str) -> list[dict]:
        return [c.output for c in self.calls if c.tool == tool and c.ok and c.output]

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------
def load_on_call(path: Path) -> list[OnCallShift]:
    if not path.exists():
        return []
    return [OnCallShift.model_validate(s) for s in json.loads(path.read_text(encoding="utf-8"))]


# --------------------------------------------------------------------------
# Toolbox
# --------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    fn: Callable[[Any], BaseModel]

    def schema(self) -> dict:
        """Tool definition in Anthropic Messages API format."""
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_model.model_json_schema()}


class Toolbox:
    def __init__(self, data: Dataset, index: PolicyIndex, on_call: list[OnCallShift],
                 memory: list[MemoryEntry] | None = None, case_id: str | None = None,
                 now: datetime | None = None, case_store: CaseMemoryStore | None = None,
                 session_id: str | None = None):
        self.tickets = {t.ticket_id: t for t in data.tickets}
        self.customers = {c.customer_id: c for c in data.customers}
        self.index = index
        self.accepted_sources: set[str] = set()  # evidence accepted so far in this run
        self.on_call = on_call
        self.memory = memory or []
        self.case_store = case_store or CaseMemoryStore()  # in-process unless a path is given
        self.session_id = session_id
        self.now = now  # fixed clock makes eval runs reproducible
        self.trace = Trace(case_id=case_id)
        self.drafts: list[BaseModel] = []
        self.tools = {t.name: t for t in [
            Tool("get_ticket", "Fetch a support ticket by ID.",
                 GetTicketInput, GetTicketOutput, self._get_ticket),
            Tool("get_customer_context",
                 "Fetch the requester of a ticket: customer record, verification and their memory.",
                 GetCustomerContextInput, GetCustomerContextOutput, self._get_customer_context),
            Tool("search_policy", "Retrieve evidence from approved support policies. If "
                 "supported is false, do not answer: ask for info, refuse, or escalate.",
                 SearchPolicyInput, SearchPolicyOutput, self._search_policy),
            Tool("check_availability", "Check who is available (on call) for a team at a given time.",
                 CheckAvailabilityInput, CheckAvailabilityOutput, self._check_availability),
            Tool("draft_ticket_update", "Draft (not send) a reply/status update on a ticket.",
                 DraftTicketUpdateInput, DraftTicketUpdateOutput, self._draft_ticket_update),
            Tool("draft_escalation", "Draft (not send) a handover of a ticket to a human team.",
                 DraftEscalationInput, DraftEscalationOutput, self._draft_escalation),
        ]}

    @classmethod
    def from_dir(cls, root: str | Path, **kwargs) -> "Toolbox":
        from validate_data import load  # validates all data first
        root = Path(root)
        return cls(load(root), PolicyIndex.from_dir(root / "knowledge_base"),
                   load_on_call(root / "operations" / "on_call.json"), **kwargs)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    # -- dispatcher -----------------------------------------------------------
    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        started, t0 = datetime.now(timezone.utc), time.perf_counter()
        output, error = None, None
        try:
            tool = self.tools.get(name)
            if tool is None:
                raise LookupError(f"unknown tool '{name}'")
            result = tool.fn(tool.input_model.model_validate(args))
            output = tool.output_model.model_validate(result.model_dump()).model_dump(mode="json")
        except ValidationError as e:
            error = "invalid input: " + "; ".join(
                f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        except Exception as e:  # noqa: BLE001 - every failure goes into the trace
            error = f"{type(e).__name__}: {e}"
        self.trace.calls.append(ToolCall(
            step=len(self.trace.calls) + 1, tool=name, input=args, output=output, error=error,
            started_at=started, duration_ms=round((time.perf_counter() - t0) * 1000, 2)))
        return output if error is None else {"error": error}

    # -- implementations -------------------------------------------------------
    def _ticket(self, ticket_id: str) -> Ticket:
        if ticket_id not in self.tickets:
            raise LookupError(f"ticket {ticket_id} not found")
        return self.tickets[ticket_id]

    def check_answer(self, text: str, require_citation: bool = True) -> list[str]:
        """Problems with a final answer's citations (empty list = OK)."""
        return citation_problems(text, self.accepted_sources, require_citation)

    def _require_valid_citations(self, text: str, require_citation: bool) -> list[str]:
        problems = self.check_answer(text, require_citation)
        if problems:
            raise ValueError("; ".join(problems))
        return extract_citations(text)

    def _remember(self, ticket_id: str, kind: str, summary: str, status: str | None,
                  cited: list[str]) -> None:
        """Record a draft in durable case memory (the store neutralizes citations)."""
        self.case_store.add(CaseMemoryEntry(
            ticket_id=ticket_id, customer_id=self.tickets[ticket_id].customer_id, kind=kind,
            summary=summary, status=status, previously_cited=cited,
            created_at=self.now or now_utc(), session_id=self.session_id))

    def _get_ticket(self, inp: GetTicketInput) -> GetTicketOutput:
        return GetTicketOutput(ticket=self._ticket(inp.ticket_id),
                               case_history=self.case_store.for_ticket(inp.ticket_id))

    def _get_customer_context(self, inp: GetCustomerContextInput) -> GetCustomerContextOutput:
        ticket = self._ticket(inp.ticket_id)
        customer = self.customers.get(ticket.customer_id)
        if customer is None:
            raise LookupError(f"customer {ticket.customer_id} not found")
        # If the ticket carries a requester email, it must match the customer record.
        requester = getattr(ticket, "requester_email", None)
        matches = requester is None or (customer.email or "").lower() == str(requester).lower()
        return GetCustomerContextOutput(
            customer=customer,
            verified=bool(getattr(customer, "verified", False)),
            matches_requester=matches,
            memory=[m for m in self.memory if m.customer_id == customer.customer_id],
            other_cases=self.case_store.for_customer(customer.customer_id,
                                                     exclude_ticket=ticket.ticket_id),
        )

    def _search_policy(self, inp: SearchPolicyInput) -> SearchPolicyOutput:
        if len(set(terms(inp.query))) < MIN_MATCHED_TERMS:
            raise ValueError(f"query too short: use at least {MIN_MATCHED_TERMS} keywords, "
                             "e.g. 'duplicate payment refund'")
        evidence, near = self.index.search(inp.query, inp.top_k)
        self.accepted_sources |= {e.source_id for e in evidence}
        guidance = ("Answer only from this evidence and cite it, e.g. [%s]." % evidence[0].source_id
                    if evidence else
                    "No approved policy supports this request. Do not answer from near misses: "
                    "use status needs_info or refused, or draft_escalation.")
        return SearchPolicyOutput(supported=bool(evidence), evidence=evidence,
                                  near_misses=near, guidance=guidance)

    def _check_availability(self, inp: CheckAvailabilityInput) -> CheckAvailabilityOutput:
        at = inp.at or self.now or datetime.now(timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        shift = next((s for s in self.on_call
                      if s.team.lower() == inp.team.lower() and s.start <= at < s.end), None)
        return CheckAvailabilityOutput(team=inp.team, at=at, available=shift is not None, on_call=shift)

    def _draft_ticket_update(self, inp: DraftTicketUpdateInput) -> DraftTicketUpdateOutput:
        self._ticket(inp.ticket_id)
        cited = self._require_valid_citations(inp.message, require_citation=inp.status == "answered")
        draft = DraftTicketUpdateOutput(draft_id=f"upd-{uuid.uuid4().hex[:8]}",
                                        created_at=self.now or datetime.now(timezone.utc),
                                        cited_sources=cited, **inp.model_dump())
        self.drafts.append(draft)
        self._remember(inp.ticket_id, "ticket_update", inp.message, inp.status, cited)
        return draft

    def _draft_escalation(self, inp: DraftEscalationInput) -> DraftEscalationOutput:
        self._ticket(inp.ticket_id)
        cited = self._require_valid_citations(inp.reason + " " + inp.summary, require_citation=False)
        draft = DraftEscalationOutput(draft_id=f"esc-{uuid.uuid4().hex[:8]}",
                                      created_at=self.now or datetime.now(timezone.utc),
                                      cited_sources=cited, **inp.model_dump())
        self.drafts.append(draft)
        self._remember(inp.ticket_id, "escalation",
                       f"Escalated to {inp.team} ({inp.priority}): {inp.reason}", "escalated", cited)
        return draft
