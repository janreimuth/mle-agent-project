"""Conversation (short-term) memory and durable case memory.

Two layers:
  * ConversationMemory: lives for one chat session. Tracks which ticket,
    customer and incident are "in focus" so follow-up turns like "the same
    customer" or "that incident" can be resolved. Cleared by /reset.
  * CaseMemoryStore: persisted to a JSON file. Records what happened on each
    ticket (drafted updates, escalations, operator notes) so a later session
    can pick the case up again.

Precedence rule: memory is context, never policy evidence.
  * Memory can tell the agent WHICH ticket/customer is meant and WHAT happened
    before. It cannot support a policy statement.
  * Case memory stores what was drafted, with citation markers like [refunds]
    rewritten to "(source refunds, re-verify)". Remembered sources must be
    retrieved again with search_policy before they can be cited, because
    accepted evidence is per session (tools.Toolbox.accepted_sources).
  * When memory and current policy evidence disagree, the evidence wins.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from retrieval import CITATION

MEMORY_NOTICE = ("Memory is context only, not policy evidence. Re-check any policy with "
                 "search_policy; if memory conflicts with current evidence, the evidence wins.")


# --------------------------------------------------------------------------
# Durable case memory
# --------------------------------------------------------------------------
class CaseMemoryEntry(BaseModel):
    ticket_id: str
    customer_id: str
    kind: Literal["ticket_update", "escalation", "note"]
    summary: str
    status: str | None = None
    previously_cited: list[str] = Field(default_factory=list,
                                        description="Source IDs cited then; must be re-verified")
    created_at: datetime
    session_id: str | None = None


class CaseMemoryStore:
    """Append-only JSON store. path=None keeps memory in-process only (for tests/evals)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.entries: list[CaseMemoryEntry] = []
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [CaseMemoryEntry.model_validate(e) for e in raw.get("entries", [])]

    def for_ticket(self, ticket_id: str) -> list[CaseMemoryEntry]:
        return [e for e in self.entries if e.ticket_id == ticket_id]

    def for_customer(self, customer_id: str, exclude_ticket: str | None = None) -> list[CaseMemoryEntry]:
        return [e for e in self.entries
                if e.customer_id == customer_id and e.ticket_id != exclude_ticket]

    def add(self, entry: CaseMemoryEntry) -> None:
        # "[refunds]" -> "(source refunds, re-verify)": remembered text can't pass as a citation.
        entry = entry.model_copy(update={
            "summary": CITATION.sub(r"(source \1, re-verify)", entry.summary)[:500]})
        self.entries.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"entries": [e.model_dump(mode="json") for e in self.entries]},
                              indent=2, ensure_ascii=False)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Short-term conversation memory
# --------------------------------------------------------------------------
_CUSTOMER_REF = re.compile(
    r"\b(?:the same|same|that|this|the)\s+(?:customer|client|requester|user)\b"
    r"|\b(?:derselbe|dieselbe|demselben|denselben|der gleiche|die gleiche|dem gleichen|"
    r"diese[mnrs]?|de[mnr])\s+(?:kunden?|kundin)\b", re.I)
_INCIDENT_REF = re.compile(
    r"\b(?:the same|same|that|this|the)\s+(?:incident|ticket|case|issue|escalation)\b"
    r"|\b(?:derselbe|dasselbe|demselben|der gleiche|das gleiche|dem gleichen|"
    r"diese[mnrs]?|de[mnr]|das)\s+(?:vorfall|vorfalls|ticket|fall|eskalation)\b", re.I)


class ConversationMemory(BaseModel):
    ticket_id: str | None = None          # ticket currently being worked on
    customer_id: str | None = None
    customer_name: str | None = None
    incident_ticket_id: str | None = None  # last escalated ticket, if any
    tickets_seen: list[str] = []
    customers_seen: list[str] = []
    events: list[str] = []                 # short log of what happened this session

    def observe(self, tool: str, output: dict) -> None:
        """Update focus from a successful tool result."""
        if tool == "get_ticket":
            t = output["ticket"]
            self._focus_ticket(t["ticket_id"], t["customer_id"])
        elif tool == "get_customer_context":
            c = output["customer"]
            self.customer_id, self.customer_name = c["customer_id"], c.get("name")
            self._seen(self.customers_seen, c["customer_id"])
        elif tool == "draft_ticket_update":
            self._focus_ticket(output["ticket_id"], None)
            self._log(f"drafted update on {output['ticket_id']} (status {output['status']})")
        elif tool == "draft_escalation":
            self._focus_ticket(output["ticket_id"], None)
            self.incident_ticket_id = output["ticket_id"]
            self._log(f"drafted escalation of {output['ticket_id']} to {output['team']}")

    def _focus_ticket(self, ticket_id: str, customer_id: str | None) -> None:
        self.ticket_id = ticket_id
        self._seen(self.tickets_seen, ticket_id)
        if customer_id:
            if customer_id != self.customer_id:
                self.customer_name = None
            self.customer_id = customer_id
            self._seen(self.customers_seen, customer_id)

    @staticmethod
    def _seen(lst: list[str], value: str) -> None:
        if value in lst:
            lst.remove(value)
        lst.append(value)

    def _log(self, event: str) -> None:
        self.events = (self.events + [event])[-10:]

    def resolve(self, user_text: str) -> list[str]:
        """Resolution hints for references in the user's message."""
        hints = []
        for m in _CUSTOMER_REF.finditer(user_text):
            if self.customer_id:
                who = f"{self.customer_id}" + (f" ({self.customer_name})" if self.customer_name else "")
                hints.append(f"'{m.group(0)}' = customer {who}")
            else:
                hints.append(f"'{m.group(0)}' is unresolved: no customer in this conversation yet, "
                             "ask the user which one")
        for m in _INCIDENT_REF.finditer(user_text):
            target = self.incident_ticket_id or self.ticket_id
            if target:
                hints.append(f"'{m.group(0)}' = ticket {target}")
            else:
                hints.append(f"'{m.group(0)}' is unresolved: no ticket in this conversation yet, "
                             "ask the user which one")
        return list(dict.fromkeys(hints))

    def annotate(self, user_text: str) -> str:
        hints = self.resolve(user_text)
        if not hints:
            return user_text
        return (user_text + "\n\n[Conversation memory, resolves references only; fetch fresh "
                "data with tools and re-check policy: " + "; ".join(hints) + "]")

    def context_block(self) -> str:
        if not (self.ticket_id or self.customer_id):
            return ""
        lines = ["Conversation memory (context only, not policy evidence):"]
        if self.ticket_id:
            lines.append(f"- current ticket: {self.ticket_id}")
        if self.customer_id:
            name = f" ({self.customer_name})" if self.customer_name else ""
            lines.append(f"- current customer: {self.customer_id}{name}")
        if self.incident_ticket_id:
            lines.append(f"- last escalated incident: ticket {self.incident_ticket_id}")
        if len(self.tickets_seen) > 1:
            lines.append(f"- tickets discussed: {', '.join(self.tickets_seen)}")
        lines += [f"- {e}" for e in self.events[-5:]]
        return "\n".join(lines)
