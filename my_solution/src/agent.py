#!/usr/bin/env python3
"""Interactive support agent on the Groq API, using the typed tools in tools.py.

Setup:   uv add groq python-dotenv
         .env:  GROQ_API_KEY=...
                GROQ_MODEL=openai/gpt-oss-20b
Run:     python agent.py [--data DIR] [--model MODEL] [--case CASE_ID]
         (--data defaults to <project root>/data, the parent of this file's folder)

The model never sees the data files directly: tickets, customers, policies and
on-call shifts are only reachable through the six typed tools, and every call is
validated against its contract and logged in the trace.

Memory:  short-term conversation memory tracks the current ticket, customer and
         incident to resolve "the same customer" / "that incident". Durable case
         memory (--case-memory, a JSON file) keeps drafts, escalations and notes
         per ticket across sessions. Neither is policy evidence.

Limits:  per user turn --max-steps, --max-tool-calls and --max-tool-errors. When one is
         reached, or the answer is unusable, the turn ends as ESCALATED (an escalation
         is drafted for the ticket in focus) or REVIEW REQUIRED (no ticket to hand off).
         Every turn prints its outcome, and the trace records it in trace.outcomes.

Chat commands:  /trace   show tool calls so far     /save PATH  write trace JSON
                /drafts  show drafts                /memory     show memory
                /note T  add a note to the current ticket's case memory
                /reset   new conversation (case memory is kept)
                /quit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from memory import CaseMemoryEntry, CaseMemoryStore, ConversationMemory, now_utc
from tools import ToolCall, Toolbox, TurnOutcome

DEFAULT_MODEL = "openai/gpt-oss-20b"  # overridden by GROQ_MODEL (env or .env) or --model
MAX_ANSWER_RETRIES = 1   # retries when a final answer has invalid citations
REVIEW_MARKER = re.compile(r"^\s*OUTCOME:\s*review_required\s*$", re.I | re.M)
MAX_HISTORY_TURNS = 12   # user turns kept verbatim; older turns live on in conversation memory

SYSTEM_PROMPT = """You are a customer support agent working on support tickets.

Rules:
- Read ticket and customer data ONLY through tools. Never invent ticket contents, \
customer details, payment references or policies.
- For any request about a ticket, call get_ticket, then get_customer_context, before answering.
- Before stating any policy or promising an action, call search_policy with a short \
keyword query (2-5 keywords).
- Cite policies with their source ID in square brackets, e.g. [refunds]. Only cite \
sources that search_policy returned as evidence in this conversation.
- If search_policy returns supported=false, do not answer from near misses: ask for \
more information, refuse, or draft an escalation (use check_availability to find an assignee).
- If customer context does not match the ticket requester or the customer is not \
verified, do not perform account-sensitive actions; escalate instead.
- draft_ticket_update and draft_escalation only create drafts; say so, nothing is sent.
- If a tool returns an error, fix the arguments or explain the problem; do not guess.

Memory:
- Conversation memory and case history help you work out WHICH ticket or customer the user means ("the same customer", "that incident") and what was done before. Still fetch current data with get_ticket / get_customer_context.
- Memory is never policy evidence. Do not state a policy because memory says so, and do not cite sources that only appear in memory: run search_policy again in this conversation.
- If memory and current policy evidence disagree, follow the evidence and say that the earlier statement is superseded.
- If a reference is unresolved, ask the user which ticket or customer they mean.

Outcomes:
- If a human must review before ANY action is taken (for example, the account owner \
cannot be verified and check_availability shows no one available to verify them), draft \
nothing. Explain why, cite the policy, and end your reply with the line: \
OUTCOME: review_required

Limits:
- Each request has a budget of model steps and tool calls. Do not repeat calls that \
already failed or already returned what you need. If you cannot finish safely, draft an \
escalation instead of retrying.
- Be concise. Answer in the user's language."""


def to_groq_tools(toolbox: Toolbox) -> list[dict]:
    """Convert the toolbox schemas into Groq/OpenAI function-tool format."""
    return [{"type": "function",
             "function": {"name": s["name"], "description": s["description"],
                          "parameters": s["input_schema"]}}
            for s in toolbox.schemas()]


@dataclass(frozen=True)
class Limits:
    """Per user turn. When one is reached the turn stops with review_required/escalated."""
    max_steps: int = 8          # model calls (including citation retries)
    max_tool_calls: int = 12    # tool calls executed
    max_tool_errors: int = 3    # failed tool calls (bad arguments, unknown IDs, rejected drafts)


class TurnResult(BaseModel):
    outcome: Literal["completed", "review_required", "escalated"]
    answer: str
    reason: str | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    answer_retries: int = 0   # answers rejected for invalid citations, then retried
    stopped: bool = False     # ended by a limit or an unusable answer, not by a decision


class SupportAgent:
    def __init__(self, client, toolbox: Toolbox, model: str = DEFAULT_MODEL, echo=print,
                 limits: Limits = Limits()):
        self.client = client
        self.toolbox = toolbox
        self.model = model
        self.echo = echo
        self.limits = limits
        self.tools = to_groq_tools(toolbox)
        self.conversation = ConversationMemory()  # short-term memory
        self.messages: list[dict] = []            # chat history, without the system prompt

    def system_prompt(self) -> str:
        context = self.conversation.context_block()
        return SYSTEM_PROMPT + ("\n\n" + context if context else "")

    def _trim_history(self) -> None:
        """Keep the last MAX_HISTORY_TURNS user turns; cut only at a user message so
        tool calls and their results stay together."""
        user_idx = [i for i, m in enumerate(self.messages) if m["role"] == "user"]
        if len(user_idx) > MAX_HISTORY_TURNS:
            del self.messages[:user_idx[-MAX_HISTORY_TURNS]]

    def _complete(self):
        self._steps += 1
        return self.client.chat.completions.create(
            model=self.model, messages=[{"role": "system", "content": self.system_prompt()}]
            + self.messages, tools=self.tools, tool_choice="auto", temperature=0,
        ).choices[0].message

    def _log_not_run(self, name: str, args: dict, error: str) -> None:
        trace = self.toolbox.trace
        trace.calls.append(ToolCall(step=len(trace.calls) + 1, tool=name, input=args, error=error,
                                    started_at=datetime.now(timezone.utc), duration_ms=0))

    def _run_tool_calls(self, tool_calls) -> str | None:
        """Run the model's tool calls within budget. Returns a stop reason if a limit is hit.
        Every call gets a tool message (skipped ones too), so the chat history stays valid."""
        self.messages.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments or "{}"}}
                           for tc in tool_calls]})
        stop = None
        for tc in tool_calls:
            name = tc.function.name
            if stop is None and self._tool_calls >= self.limits.max_tool_calls:
                stop = f"tool-call limit reached ({self.limits.max_tool_calls} per turn)"
            if stop:
                args = {"raw_arguments": tc.function.arguments}
                result = {"error": f"not executed: {stop}"}
                self._log_not_run(name, args, result["error"])
                self.echo(f"  · {name} -> skipped ({stop})")
            else:
                self._tool_calls += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                    result = self.toolbox.call(name, args)
                except ValueError as e:  # malformed arguments: trace it, tool is not run
                    args = {"raw_arguments": tc.function.arguments}
                    result = {"error": f"invalid JSON arguments: {e}"}
                    self._log_not_run(name, args, result["error"])
                call = self.toolbox.trace.calls[-1]
                if call.ok:
                    self.conversation.observe(name, result)
                else:
                    self._tool_errors += 1
                    if self._tool_errors >= self.limits.max_tool_errors:
                        stop = f"too many failed tool calls ({self._tool_errors} in this turn)"
                mark = "ok" if call.ok else f"error: {call.error}"
                self.echo(f"  · {name}({json.dumps(args, ensure_ascii=False)}) -> {mark}")
            self.messages.append({"role": "tool", "tool_call_id": tc.id,
                                  "content": json.dumps(result, ensure_ascii=False)})
        return stop

    def _escalated_this_turn(self) -> dict | None:
        drafts = [c.output for c in self.toolbox.trace.calls[self._first_call - 1:]
                  if c.tool == "draft_escalation" and c.ok]
        return drafts[-1] if drafts else None

    def _finish(self, outcome: str, answer: str, reason: str | None = None,
                fallback: bool = False, stopped: bool = False) -> TurnResult:
        self.messages.append({"role": "assistant", "content": answer})
        result = TurnResult(outcome=outcome, answer=answer, reason=reason, steps=self._steps,
                            tool_calls=self._tool_calls, tool_errors=self._tool_errors,
                            answer_retries=self._retries, stopped=stopped)
        self.toolbox.trace.outcomes.append(TurnOutcome(
            **result.model_dump(exclude={"answer"}), first_call=self._first_call,
            fallback_escalation=fallback))
        return result

    def _stop_unsafe(self, reason: str, user_text: str) -> TurnResult:
        """Bounds reached or answer unusable: hand off instead of guessing.
        With a ticket in focus the app drafts an escalation (traced like any tool call);
        otherwise there is nothing to hand off and a human must review the conversation."""
        if (esc := self._escalated_this_turn()):
            return self._finish("escalated", f"Stopped: {reason}. Escalation {esc['draft_id']} "
                                f"of {esc['ticket_id']} to {esc['team']} was already drafted.", reason,
                                stopped=True)
        ticket_id = self.conversation.ticket_id
        if ticket_id:
            team = self.toolbox.tickets[ticket_id].category
            on_call = self.toolbox.call("check_availability", {"team": team})
            assignee = (on_call.get("on_call") or {}).get("name")
            events = "; ".join(self.conversation.events[-3:]) or "no drafts yet"
            esc = self.toolbox.call("draft_escalation", {
                "ticket_id": ticket_id, "team": team, "priority": "normal", "assignee": assignee,
                "reason": f"Agent stopped: {reason}",
                "summary": f"Request: {user_text[:300]} | Done so far: {events}"})
            if "error" not in esc:
                self.conversation.observe("draft_escalation", esc)
                who = f", assigned to {assignee}" if assignee else ", no one on call"
                return self._finish(
                    "escalated", f"I could not complete this safely ({reason}). I drafted "
                    f"escalation {esc['draft_id']} of {ticket_id} to {team}{who}.", reason,
                    fallback=True, stopped=True)
        return self._finish(
            "review_required", f"I could not complete this safely ({reason}). "
            "A human needs to review this conversation; no ticket was identified to escalate.",
            reason, stopped=True)

    def ask(self, user_text: str) -> TurnResult:
        self.messages.append({"role": "user", "content": self.conversation.annotate(user_text)})
        self._trim_history()
        self._steps = self._tool_calls = self._tool_errors = self._retries = 0
        self._first_call = len(self.toolbox.trace.calls) + 1
        while self._steps < self.limits.max_steps:
            msg = self._complete()
            if msg.tool_calls:
                stop = self._run_tool_calls(msg.tool_calls)
                if stop:
                    return self._stop_unsafe(stop, user_text)
                continue
            answer = (msg.content or "").strip()
            wants_review = bool(REVIEW_MARKER.search(answer))
            answer = REVIEW_MARKER.sub("", answer).strip()
            if not answer and wants_review:
                answer = "This request needs human review before any action."
            if not answer:
                return self._stop_unsafe("the model returned an empty answer", user_text)
            problems = self.toolbox.check_answer(answer, require_citation=False)
            if problems and self._retries < MAX_ANSWER_RETRIES:
                self._retries += 1
                self.messages.append({"role": "assistant", "content": answer})
                self.messages.append({"role": "user", "content":
                    "Your answer was rejected: " + "; ".join(problems) +
                    ". Rewrite it citing only sources returned as evidence in this "
                    "conversation. Sources remembered from earlier work are not evidence: "
                    "run search_policy first."})
                continue
            if problems:
                return self._stop_unsafe("answer cites sources without accepted evidence",
                                         user_text)
            if self._escalated_this_turn():
                return self._finish("escalated", answer)
            if wants_review:  # a deliberate decision, not a stop
                return self._finish("review_required", answer, reason="agent requested human review")
            return self._finish("completed", answer)
        return self._stop_unsafe(f"step limit reached ({self.limits.max_steps} model calls)",
                                 user_text)

    def reset(self, toolbox: Toolbox) -> None:
        """New conversation: clears chat history, short-term memory and accepted evidence."""
        self.toolbox = toolbox
        self.conversation = ConversationMemory()
        self.messages = []


OUTCOME_LABEL = {"completed": "COMPLETED", "escalated": "ESCALATED",
                 "review_required": "REVIEW REQUIRED"}


def print_result(r: TurnResult) -> None:
    print(f"\nagent> {r.answer}")
    stats = f"{r.steps} steps, {r.tool_calls} tool calls, {r.tool_errors} errors"
    line = f"[{OUTCOME_LABEL[r.outcome]}] {stats}" + (f" | reason: {r.reason}" if r.reason else "")
    print(line if r.outcome == "completed" else f"\n!!! {line}")


def build_toolbox(args, case_store: CaseMemoryStore, session_id: str) -> Toolbox:
    memory, case_id = [], None
    if args.case:
        from validate_data import load
        case = next((c for c in load(args.data).cases if c.case_id == args.case), None)
        if case is None:
            sys.exit(f"Unknown case: {args.case}")
        memory, case_id = case.seeded_memory, case.case_id
    return Toolbox.from_dir(args.data, memory=memory, case_id=case_id,
                            case_store=case_store, session_id=session_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="Interactive support agent (Groq)")
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    ap.add_argument("--data", type=Path, default=PROJECT_ROOT / "data",
                    help="data directory (default: <project root>/data)")
    ap.add_argument("--model", help="Groq model (default: GROQ_MODEL from env/.env, "
                    f"else {DEFAULT_MODEL})")
    ap.add_argument("--case", help="load seeded memory from this evaluation case")
    ap.add_argument("--max-steps", type=int, default=Limits.max_steps,
                    help=f"model calls per turn (default {Limits.max_steps})")
    ap.add_argument("--max-tool-calls", type=int, default=Limits.max_tool_calls,
                    help=f"tool calls per turn (default {Limits.max_tool_calls})")
    ap.add_argument("--max-tool-errors", type=int, default=Limits.max_tool_errors,
                    help=f"failed tool calls per turn (default {Limits.max_tool_errors})")
    ap.add_argument("--case-memory", type=Path,
                    default=PROJECT_ROOT / ".agent_memory" / "case_memory.json",
                    help="durable case memory file (default: <project root>/.agent_memory/"
                         "case_memory.json)")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv  # optional: read GROQ_API_KEY from .env
        load_dotenv()
    except ImportError:
        if (PROJECT_ROOT / ".env").exists() or Path(".env").exists():
            print("Warning: .env found but python-dotenv is not installed; it is ignored. "
                  "Run: uv add python-dotenv")
    args.model = args.model or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
    if not os.environ.get("GROQ_API_KEY"):
        sys.exit("Set GROQ_API_KEY (environment variable or .env file).")
    from groq import Groq, GroqError

    case_store = CaseMemoryStore(args.case_memory)
    session_id = uuid.uuid4().hex[:8]
    try:
        toolbox = build_toolbox(args, case_store, session_id)
    except Exception as e:  # noqa: BLE001 - invalid data should stop the agent before it starts
        sys.exit(f"Data validation failed, run validate_data.py for details:\n{e}")
    limits = Limits(args.max_steps, args.max_tool_calls, args.max_tool_errors)
    agent = SupportAgent(Groq(), toolbox, model=args.model, limits=limits)

    print(f"Support agent ({args.model}). {len(toolbox.tickets)} tickets, "
          f"{len(toolbox.index.source_ids)} policies, {len(case_store.entries)} case memory "
          f"entries. Type /quit to exit.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/reset":
            session_id = uuid.uuid4().hex[:8]
            toolbox = build_toolbox(args, case_store, session_id)
            agent.reset(toolbox)
            print("New conversation: history, short-term memory, evidence and trace cleared. "
                  "Case memory kept.")
            continue
        if text == "/memory":
            print(agent.conversation.context_block() or "No conversation memory yet.")
            tid = agent.conversation.ticket_id
            for e in case_store.for_ticket(tid) if tid else []:
                print(f"  [{e.created_at:%Y-%m-%d %H:%M}] {tid} {e.kind}: {e.summary}")
            continue
        if text.startswith("/note"):
            tid = agent.conversation.ticket_id
            note = text[len("/note"):].strip()
            if not tid or not note:
                print("Usage: /note TEXT (after a ticket has been looked up)")
                continue
            case_store.add(CaseMemoryEntry(
                ticket_id=tid, customer_id=toolbox.tickets[tid].customer_id, kind="note",
                summary=note, created_at=now_utc(), session_id=session_id))
            print(f"Note added to case memory for {tid}.")
            continue
        if text == "/trace":
            for c in toolbox.trace.calls:
                print(f"  {c.step:>2}. {c.tool:<22} {'ok' if c.ok else 'ERROR ' + c.error}")
            continue
        if text == "/drafts":
            for d in toolbox.drafts:
                print(json.dumps(d.model_dump(mode="json"), indent=2, ensure_ascii=False))
            continue
        if text.startswith("/save"):
            path = text.split(maxsplit=1)[1] if " " in text else (
                f"trace-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json")
            toolbox.trace.save(path)
            print(f"Trace saved to {path}")
            continue
        history_len = len(agent.messages)
        try:
            print_result(agent.ask(text))
        except GroqError as e:
            del agent.messages[history_len:]  # roll back the failed turn; the trace keeps its calls
            print(f"\n[Groq API error: {e}]")


if __name__ == "__main__":
    main()
