#!/usr/bin/env python3
"""Validate data/ before running the agent. Usage: python validate_data.py [data_dir]"""
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated

from retrieval import PolicyIndex
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Ticket(BaseModel):
    model_config = ConfigDict(extra="allow")
    ticket_id: NonEmpty
    customer_id: NonEmpty
    category: NonEmpty
    subject: NonEmpty
    body: NonEmpty


class Customer(BaseModel):
    model_config = ConfigDict(extra="allow")
    customer_id: NonEmpty
    name: str | None = None
    email: str | None = None


class MemoryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    customer_id: NonEmpty
    text: str = ""


class AgentCase(BaseModel):
    model_config = ConfigDict(extra="allow")
    case_id: NonEmpty
    ticket_id: NonEmpty | None = None  # None = case not tied to a ticket
    prompt: str = ""
    expected_status: str | None = None
    expected_tools: list[NonEmpty] = []
    expected_sources: list[NonEmpty] = []
    must_use_memory: bool = False
    seeded_memory: list[MemoryEntry] = []


def dupes(values) -> list:
    return [v for v, n in Counter(values).items() if n > 1]


class Dataset(BaseModel):
    tickets: list[Ticket]
    customers: list[Customer]
    cases: list[AgentCase]
    sources: set[str]

    @model_validator(mode="after")
    def check_references(self):
        customers = {c.customer_id: c for c in self.customers}
        owner = {t.ticket_id: t.customer_id for t in self.tickets}

        assert not (d := dupes(t.ticket_id for t in self.tickets)), f"duplicate ticket_id: {d}"
        assert not (d := dupes(c.customer_id for c in self.customers)), f"duplicate customer_id: {d}"

        # 2. Every referenced customer exists.
        bad = [(t.ticket_id, t.customer_id) for t in self.tickets if t.customer_id not in customers]
        assert not bad, f"tickets reference unknown customers (ticket, customer): {bad}"

        # 3. Case IDs are unique and tickets exist.
        assert not (d := dupes(c.case_id for c in self.cases)), f"duplicate case_id: {d}"
        bad = [(c.case_id, c.ticket_id) for c in self.cases if c.ticket_id is not None and c.ticket_id not in owner]
        assert not bad, f"cases reference unknown tickets (case, ticket): {bad}"

        # 4. Expected sources exist in the knowledge base.
        bad = [(c.case_id, s) for c in self.cases for s in c.expected_sources if s not in self.sources]
        assert not bad, f"expected sources missing from knowledge_base (case, source): {bad}"

        # 5. Seeded memory only concerns the ticket's own customer.
        for c in self.cases:
            case_owner = owner.get(c.ticket_id)  # None for ticketless cases
            for m in c.seeded_memory:
                assert m.customer_id == case_owner, (
                    f"case {c.case_id}: memory for {m.customer_id}, ticket belongs to {case_owner}")
                text = m.model_dump_json().lower()
                leaked = [(oid, v) for oid, o in customers.items() if oid != case_owner
                          for v in (oid, o.email, o.name) if v and v.lower() in text]
                assert not leaked, f"case {c.case_id}: memory mentions other customers: {leaked}"
        return self


def load(root: str | Path) -> Dataset:
    root = Path(root)
    read = lambda p: json.loads((root / p).read_text(encoding="utf-8"))
    sources = PolicyIndex.from_dir(root / "knowledge_base").source_ids  # approved sources only
    return Dataset(
        tickets=read("operations/tickets.json"),
        customers=read("operations/customers.json"),
        cases=read("evaluation/agent_cases.json"),
        sources=sources,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "data"
    try:
        data = load(data_dir)
    except ValidationError as e:
        for err in e.errors():
            where = ".".join(map(str, err["loc"]))
            msg = err["msg"].removeprefix("Assertion failed, ")
            got = "" if err["type"].startswith("assertion") else f" (got {err['input']!r})"
            print(f"ERROR {where}: {msg}{got}" if where else f"ERROR {msg}")
        sys.exit(1)
    print(f"All checks passed: {len(data.tickets)} tickets, {len(data.customers)} customers, "
          f"{len(data.cases)} cases, {len(data.sources)} sources.")
