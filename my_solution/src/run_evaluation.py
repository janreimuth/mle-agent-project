#!/usr/bin/env python3
"""Run every evaluation case through the agent and write reports/evaluation_report.md.

Usage:  uv run src/run_evaluation.py [--repeats 3] [--model MODEL] [--now 2026-11-03T10:00:00Z]

Each case runs in a fresh session: new toolbox, the case's seeded memory, in-process
case memory and the configured limits. The first user message is the case prompt; if
the case has a ticket_id that the prompt doesn't mention, "Ticket <id>:" is prefixed so
the agent knows which ticket is meant. A case may list follow-up messages in an optional
"turns" field, which are sent after the prompt in the same session.

Outputs: reports/evaluation_report.md, reports/results.json, reports/traces/<case>__run<n>.json

Without tokens:
  --mock oracle   a scripted model plays every case as expected; all cases should pass
  --mock faults   the scripted model makes one known mistake per run; each must be caught
  (mock output goes to reports/mock/, real results are not touched)

Token budget: Groq's free tier has a daily token limit. When it is reached the runner stops
calling the API, marks the remaining runs "not run" and still writes the report. Then:
  --resume    reuse completed runs from the last results.json, run only missing/failed ones
  --rescore   no API calls at all: rescore the saved runs with the current evaluator
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import retrieval
from agent import DEFAULT_MODEL, MAX_ANSWER_RETRIES, MAX_HISTORY_TURNS, Limits, SupportAgent, TurnResult
from evaluate_trace import ABSTAIN, ToolEvaluation, evaluate
from memory import CaseMemoryStore
from retrieval import PolicyIndex
from tools import Toolbox, Trace
from validate_data import AgentCase, load

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SWEEP_MATCHED = (1, 2, 3)
SWEEP_COVERAGE = (0.4, 0.5, 0.6, 0.7, 0.8)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
@dataclass
class Run:
    case: AgentCase
    run: int
    turns: list[TurnResult]
    trace: Trace
    eval: ToolEvaluation | None
    error: str | None          # infrastructure error (API, crash), not an agent decision
    trace_path: str
    quota_exhausted: bool = False
    signals: list[str] = field(default_factory=list)   # fragility signals
    categories: list[str] = field(default_factory=list)  # failure categories


def case_messages(case: AgentCase) -> list[str]:
    first = case.prompt or "Please handle this ticket."
    if case.ticket_id and case.ticket_id not in first:
        first = f"Ticket {case.ticket_id}: {first}"
    extra = [t for t in (case.model_extra or {}).get("turns", []) if isinstance(t, str)]
    return [first, *extra]


def is_daily_limit(exc: Exception) -> bool:
    text = str(exc)
    return type(exc).__name__ == "RateLimitError" and ("per day" in text or "(TPD)" in text)


def not_run(case: AgentCase, run_no: int, reason: str) -> Run:
    return Run(case, run_no, [], Trace(case_id=case.case_id), None, f"not run: {reason}", "")


def run_case(client, case: AgentCase, run_no: int, args, out_dir: Path) -> Run:
    client = client(case) if callable(client) else client  # a factory gives one client per case
    tb = Toolbox.from_dir(args.data, memory=case.seeded_memory, case_id=case.case_id,
                          now=args.now, case_store=CaseMemoryStore(),
                          session_id=f"eval-{run_no}")
    agent = SupportAgent(client, tb, model=args.model, echo=lambda *_: None, limits=args.limits)
    turns, error, quota = [], None, False
    for msg in case_messages(case):
        try:
            turns.append(agent.ask(msg))
        except Exception as e:  # noqa: BLE001 - recorded as an infrastructure error
            error, quota = f"{type(e).__name__}: {e}", is_daily_limit(e)
            break
    # A failed run gets its own file so it never overwrites the trace of a good run.
    name = f"{case.case_id}__run{run_no}" + (".error" if error else "") + ".json"
    path = out_dir / "traces" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tb.trace.save(path)
    ev = evaluate(case, tb.trace, final_answer=turns[-1].answer) if turns and not error else None
    return Run(case, run_no, turns, tb.trace, ev, error, path.relative_to(out_dir).as_posix(),
               quota_exhausted=quota)


def load_previous(args, index: PolicyIndex, cases: list[AgentCase]) -> dict[tuple, Run]:
    """Completed runs from the last results.json, rescored with the current evaluator."""
    path = args.out / "results.json"
    if not path.exists():
        return {}
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict):
        print("Previous results.json has the old format; nothing to reuse.")
        return {}
    old, new = saved.get("config", {}), run_config(args, index)
    changed = [k for k in ("model", "limits", "retrieval", "policy_sources") if old.get(k) != new[k]]
    if changed:
        print(f"Not reusing previous runs: {', '.join(changed)} changed since they were made.")
        return {}
    by_id = {c.case_id: c for c in cases}
    reused = {}
    for r in saved.get("runs", []):
        case, trace_file = by_id.get(r["case_id"]), args.out / (r.get("trace") or "")
        if r.get("error") or not case or not r.get("turns") or not trace_file.is_file():
            continue
        turns = [TurnResult(**t) for t in r["turns"]]
        trace = Trace.model_validate_json(trace_file.read_text(encoding="utf-8"))
        reused[(case.case_id, r["run"])] = Run(
            case, r["run"], turns, trace, evaluate(case, trace, final_answer=turns[-1].answer),
            None, r["trace"])
    return reused


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def task_ok(r: Run) -> bool:
    e = r.eval
    return bool(e) and not e.stopped_by_limit and e.status_ok


def tools_ok(r: Run, ignore_failed: bool = False) -> bool:
    e = r.eval
    return bool(e) and not (e.missing_tools or e.unexpected_tools) and e.order_ok and (
        ignore_failed or not e.failed_calls)


def grounding_ok(r: Run) -> bool:
    e, c = r.eval, r.case
    if not e or e.citation_errors or e.missing_sources:
        return False
    unsupported = not c.expected_sources and c.expected_status in ABSTAIN
    return not unsupported or e.abstained  # unsupported requests must abstain or hand off


def memory_ok(r: Run) -> bool | None:
    return bool(r.eval and r.eval.memory_used) if r.case.must_use_memory else None


def had_trouble(r: Run) -> bool:
    return bool(r.eval) and bool(r.eval.failed_calls or r.eval.stopped_by_limit
                                 or any(t.answer_retries for t in r.turns))


def recovered(r: Run) -> bool:
    """Reached the right result despite failed calls or rejected answers."""
    return task_ok(r) and tools_ok(r, ignore_failed=True) and grounding_ok(r)


def cited_coverage(r: Run) -> float | None:
    if not r.eval or not r.eval.cited_sources:
        return None
    cov = [e["coverage"] for o in r.trace.outputs("search_policy") for e in o["evidence"]
           if e["source_id"] in r.eval.cited_sources]
    return min(cov) if cov else None


def classify(r: Run, limits: Limits, pass_rate: float) -> None:
    e = r.eval
    if r.error:
        if not r.error.startswith("not run"):
            r.categories.append("infrastructure error")
        return
    real_failures = [f for f in e.failed_calls if "not executed:" not in f]
    if e.stopped_by_limit:
        # Root cause only: the fallback escalation and skipped calls follow from the stop.
        r.categories.append("stopped at limit")
        if real_failures:
            r.categories.append("tool errors")
    elif e.missing_tools or e.unexpected_tools or not e.order_ok:
        r.categories.append("tool selection")
    if real_failures and not e.stopped_by_limit:
        r.categories.append("tool errors")
    if (e.missing_sources or e.citation_errors) and not e.stopped_by_limit:
        r.categories.append("grounding")
    if not r.case.expected_sources and r.case.expected_status in ABSTAIN and not e.abstained:
        r.categories.append("answered unsupported request")
    if r.case.must_use_memory and not e.memory_used:
        r.categories.append("memory not used")
    if not e.status_ok and not e.stopped_by_limit:
        r.categories.append("wrong status")
    steps = sum(t.steps for t in r.turns)
    calls = sum(t.tool_calls for t in r.turns)
    if real_failures:
        r.signals.append(f"{len(real_failures)} failed tool call(s)")
    if any(t.answer_retries for t in r.turns):
        r.signals.append("answer rejected for citations and retried")
    if max((t.steps for t in r.turns), default=0) >= 0.75 * limits.max_steps:
        r.signals.append(f"used {steps} of {limits.max_steps} model steps")
    if max((t.tool_calls for t in r.turns), default=0) >= 0.75 * limits.max_tool_calls:
        r.signals.append(f"used {calls} of {limits.max_tool_calls} tool calls")
    cov = cited_coverage(r)
    if cov is not None and cov < retrieval.MIN_COVERAGE + 0.1:
        r.signals.append(f"cited evidence coverage {cov:.2f}, close to the "
                         f"{retrieval.MIN_COVERAGE} threshold")
    if pass_rate < 1:
        r.signals.append(f"case passed in {pass_rate:.0%} of repeats")


# --------------------------------------------------------------------------
# Retrieval threshold evidence
# --------------------------------------------------------------------------
def labeled_queries(runs: list[Run], cases: list[AgentCase], data) -> list[dict]:
    """Queries labeled positive (case expects sources) or negative (unsupported case).
    Ticket subjects are added as probes for supported cases only."""
    tickets = {t.ticket_id: t for t in data.tickets}
    out, seen = [], set()

    def add(query, case, origin):
        if origin == "ticket subject probe" and not case.expected_sources:
            return  # the ticket's subject is not the unsupported request itself
        if case.expected_sources:
            label = "supported"
        elif case.expected_status in ABSTAIN:
            label = "unsupported"
        else:
            return
        key = (query.strip().lower(), tuple(sorted(case.expected_sources)))
        if key not in seen:
            seen.add(key)
            out.append({"query": query, "case": case.case_id, "label": label,
                        "expected": case.expected_sources, "origin": origin})

    for r in runs:
        for c in r.trace.calls:
            if c.tool == "search_policy" and isinstance(c.input.get("query"), str):
                add(c.input["query"], r.case, "agent query")
    for case in cases:
        t = tickets.get(case.ticket_id or "")
        if t:
            add(t.subject, case, "ticket subject probe")
    return out


def sweep(index: PolicyIndex, queries: list[dict]) -> list[dict]:
    rows = []
    for mm in SWEEP_MATCHED:
        for cov in SWEEP_COVERAGE:
            pos = [q for q in queries if q["label"] == "supported"]
            neg = [q for q in queries if q["label"] == "unsupported"]
            hit = sum(any(e.source_id in q["expected"] for e in
                          index.search(q["query"], 10, mm, cov)[0]) for q in pos)
            fa = sum(bool(index.search(q["query"], 10, mm, cov)[0]) for q in neg)
            rows.append({"matched": mm, "coverage": cov, "pos": len(pos), "hit": hit,
                         "neg": len(neg), "false_accept": fa,
                         "current": mm == retrieval.MIN_MATCHED_TERMS
                         and abs(cov - retrieval.MIN_COVERAGE) < 1e-9})
    return rows


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def compress(items: list[str]) -> str:
    """'a → b → b → b' becomes 'a → b ×3'."""
    out = []
    for x in items:
        if out and out[-1][0] == x:
            out[-1][1] += 1
        else:
            out.append([x, 1])
    return " → ".join(f"{x} ×{n}" if n > 1 else x for x, n in out)


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else "n/a (no applicable runs)"


def cell(v) -> str:
    s = ", ".join(v) if isinstance(v, list) else ("" if v is None else str(v))
    return s.replace("|", "\\|").replace("\n", " ") or "–"


RECOMMENDATIONS = {
    "stopped at limit": (
        "Cut the work per turn before raising limits",
        "Turns hit the step or tool-call budget. Inspect their traces for repeated "
        "identical calls; add a duplicate-call guard in `SupportAgent._run_tool_calls` that "
        "returns the cached result, and tell the model in the system prompt which tools "
        "a ticket request needs. Raise `Limits` only if traces show legitimate work cut off."),
    "tool selection": (
        "Make the expected tool sequence explicit",
        "Runs missed, reordered or added tools. State the standard sequence in the system "
        "prompt (get_ticket -> get_customer_context -> search_policy -> draft) and add one "
        "worked example; check whether the case's `expected_tools` is too strict first."),
    "tool errors": (
        "Reduce invalid tool arguments",
        "Tool calls failed on their contracts. Add examples to the pydantic `Field` "
        "descriptions (they reach the model as JSON schema) and check whether the failures "
        "cluster on one tool or one argument."),
    "grounding": (
        "Improve evidence retrieval and citation",
        "Expected sources were not cited or citations were rejected. Compare the agent's "
        "search queries in the threshold table with the policy wording; add query guidance "
        "(2-5 keywords from the ticket) or synonyms to the policies before lowering the threshold."),
    "answered unsupported request": (
        "Enforce abstention for unsupported requests",
        "The agent answered although no policy supported the request. Reject "
        "`draft_ticket_update(status='answered')` when no `search_policy` call in this turn "
        "returned evidence, instead of relying on the prompt."),
    "memory not used": (
        "Surface memory where the model looks",
        "Cases that need memory did not use it. Check the trace for get_customer_context; "
        "if it was called, the model ignored the memory field, so mention seeded memory in "
        "the context block of the system prompt."),
    "wrong status": (
        "Align final status with the case expectation",
        "The final draft status differed from `expected_status`. Define in the system prompt "
        "when each status applies (answered / needs_info / refused / escalated)."),
    "infrastructure error": (
        "Stabilize the evaluation harness",
        "Runs crashed or the API failed. Check rate limits (Groq 429) and the error messages; "
        "these runs say nothing about agent quality and should be re-run."),
}


def recommendations(runs: list[Run], queries: list[dict], repeats: int) -> list[tuple]:
    affected: dict[str, set] = defaultdict(set)
    for r in runs:
        for c in r.categories:
            affected[c].add(r.case.case_id)
    recs = [(RECOMMENDATIONS[c][0], RECOMMENDATIONS[c][1],
             f"{len(ids)} case(s): {', '.join(sorted(ids))}")
            for c, ids in sorted(affected.items(), key=lambda kv: -len(kv[1]))]
    labels = Counter(q["label"] for q in queries)
    preventive = []
    if not labels["unsupported"]:
        preventive.append((
            "Add unsupported-request cases",
            "The evaluation set has no case without supporting policy that expects "
            "`needs_info`, `refused` or `escalated`, so the false-accept rate of the retrieval "
            "threshold and the abstention behaviour are unmeasured.",
            "no negative cases in the set"))
    if repeats < 3:
        preventive.append((
            "Run each case at least 3 times",
            "With fewer than 3 repeats, model nondeterminism is invisible and fragile "
            "successes cannot be detected. Use `--repeats 3` and report pass^k.",
            f"current repeats: {repeats}"))
    if not any(r.case.must_use_memory for r in runs):
        preventive.append((
            "Add memory cases",
            "No case sets `must_use_memory`, so the memory-use score is not measured. Add a "
            "case with seeded memory and a follow-up turn such as 'the same customer'.",
            "no memory cases in the set"))
    n_cases = len({r.case.case_id for r in runs})
    if labels["unsupported"] and labels["unsupported"] < 5:
        preventive.append((
            "Add more unsupported queries before trusting the threshold",
            f"The threshold's false-accept rate rests on {labels['unsupported']} unsupported "
            "quer(ies). Add cases whose requests are close to, but not covered by, a policy "
            "(e.g. account deletion vs. account access) so the sweep can separate settings.",
            f"{labels['unsupported']} negative quer(ies)"))
    if n_cases < 20:
        preventive.append((
            "Grow the evaluation set",
            f"With {n_cases} case(s), one run changes a score by "
            f"{100 / max(len(runs), 1):.0f} percentage points. Aim for at least 20 cases covering "
            "each ticket category, each policy, unsupported requests and multi-turn follow-ups.",
            f"{n_cases} case(s)"))
    return (recs + preventive)[:3]


def write_report(path: Path, runs: list[Run], cases: list[AgentCase], args, index: PolicyIndex,
                 queries: list[dict], rows: list[dict]) -> None:
    ok_runs = [r for r in runs if r.eval]
    L = []
    L += ["# Evaluation Report", "",
          f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `run_evaluation.py`. "
          "All numbers below are computed from the runs listed in the per-case table; "
          "traces are in `reports/traces/`.", ""]

    # 1. Configuration
    commit = git_commit()
    lim = args.limits
    L += ["## 1. Configuration and agent-control limits", "",
          "| Setting | Value |", "|---|---|",
          f"| Model | `{args.model}` (Groq, temperature 0) |",
          f"| Git commit | `{commit}` |",
          f"| Cases × repeats | {len(cases)} × {args.repeats} = {len(runs)} runs |",
          f"| Clock for on-call lookups | {args.now.isoformat() if args.now else 'real time'} |",
          f"| Policy corpus | {len(index.source_ids)} approved sources, {len(index.passages)} passages |",
          f"| Max model steps per turn | {lim.max_steps} |",
          f"| Max tool calls per turn | {lim.max_tool_calls} |",
          f"| Max failed tool calls per turn | {lim.max_tool_errors} |",
          f"| Citation retries per answer | {MAX_ANSWER_RETRIES} |",
          f"| Chat history kept | last {MAX_HISTORY_TURNS} user turns |",
          f"| Evidence threshold | ≥ {retrieval.MIN_MATCHED_TERMS} matched query terms and "
          f"≥ {retrieval.MIN_COVERAGE:.0%} query coverage, ranked by BM25 "
          f"(k1={retrieval.BM25_K1}, b={retrieval.BM25_B}) |",
          "| Stop behaviour | at a limit or unusable answer: escalation drafted for the "
          "ticket in focus (`escalated`), otherwise `review_required` |", ""]

    # 2. Aggregate scores
    trouble = [r for r in ok_runs if had_trouble(r)]
    stops = [r for r in ok_runs if r.eval.stopped_by_limit]
    mem = [r for r in ok_runs if memory_ok(r) is not None]
    by_case = defaultdict(list)
    for r in ok_runs:
        by_case[r.case.case_id].append(r.eval.passed)
    L += ["## 2. Aggregate scores", "",
          f"Over {len(ok_runs)} completed runs"
          + (f" ({len(runs) - len(ok_runs)} run(s) excluded for infrastructure errors)"
             if len(ok_runs) < len(runs) else "") + ".", "",
          "| Dimension | Score | Definition |", "|---|---|---|",
          f"| Task completion | {pct(sum(task_ok(r) for r in ok_runs), len(ok_runs))} | final "
          "status matches `expected_status` and no turn stopped at a limit |",
          f"| Tool correctness | {pct(sum(tools_ok(r) for r in ok_runs), len(ok_runs))} | all "
          "expected tools called in order, no unexpected tools, no failed calls |",
          f"| Retrieval grounding | {pct(sum(grounding_ok(r) for r in ok_runs), len(ok_runs))} | "
          "expected sources cited, no citation outside accepted evidence, unsupported "
          "requests abstained or handed off |",
          f"| Memory use | {pct(sum(memory_ok(r) for r in mem), len(mem))} | cases with "
          "`must_use_memory`: memory was returned to the agent |",
          f"| Failure recovery | {pct(sum(recovered(r) for r in trouble), len(trouble))} | runs "
          "with failed calls, rejected answers or limit stops that still reached the right result |",
          f"| Safe stops | {pct(sum(r.eval.final_outcome in ('escalated', 'review_required') for r in stops), len(stops))} "
          "| limit stops that ended as `escalated` or `review_required` |",
          f"| Overall pass | {pct(sum(r.eval.passed for r in ok_runs), len(ok_runs))} | all checks "
          "of `evaluate_trace.evaluate` |",
          f"| Consistent cases (pass^k) | {pct(sum(all(v) for v in by_case.values()), len(by_case))} "
          f"| cases that passed in all {args.repeats} repeat(s) |", ""]

    # 3. Per-case table
    L += ["## 3. Per-case results", "",
          "| Case | Run | Outcome | Status exp → got | Missing / unexpected tools | "
          "Sources exp → cited | Memory | Steps / calls / errors | Pass |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        if not r.eval:
            L.append(f"| {cell(r.case.case_id)} | {r.run} | error | – | – | – | – | – | "
                     f"✗ {cell(r.error)} |")
            continue
        e = r.eval
        tools = " / ".join([cell(e.missing_tools), cell(e.unexpected_tools)])
        mem_cell = ("used" if e.memory_used else "not used") if r.case.must_use_memory else \
            ("used" if e.memory_used else "–")
        st = sum(t.steps for t in r.turns)
        tc = sum(t.tool_calls for t in r.turns)
        er = sum(t.tool_errors for t in r.turns)
        L.append(f"| {cell(r.case.case_id)} | {r.run} | {cell(e.final_outcome)} | "
                 f"{cell(e.expected_status)} → {cell(e.final_status)} | {tools} | "
                 f"{cell(e.expected_sources)} → {cell(e.cited_sources)} | {mem_cell} | "
                 f"{st} / {tc} / {er} | {'✓' if e.passed else '✗'} |")
    L.append("")

    # 4. Analyses
    failures = [r for r in runs if (r.error and not r.error.startswith("not run"))
                or (r.eval and not r.eval.passed)]
    fragile = sorted((r for r in ok_runs if r.eval.passed and r.signals),
                     key=lambda r: -len(r.signals))
    ordered, seen_cases = [], set()
    for r in failures + fragile:  # one run per case first, then remaining runs
        if r.case.case_id not in seen_cases:
            seen_cases.add(r.case.case_id)
            ordered.append(r)
    ordered += [r for r in failures + fragile if r not in ordered]
    picked = ordered[:max(2, min(len(seen_cases), 4))]
    L += ["## 4. Failure and fragile-success analyses", ""]
    if len(picked) < 2:
        L += [f"Only {len(picked)} failing or fragile run(s) were found, fewer than the two "
              "required analyses. Fragile means: passed, but with failed calls, a rejected "
              "answer, ≥75% of a budget used, cited evidence near the threshold, or failing "
              "in another repeat. Run more repeats or add harder cases.", ""]
    for i, r in enumerate(picked, 1):
        kind = "Failure" if (r.error or not r.eval.passed) else "Fragile success"
        L += [f"### 4.{i} {kind}: `{r.case.case_id}` (run {r.run})", ""]
        if r.error:
            L += [f"The run did not finish: `{r.error}`. This is a harness or API problem, "
                  "not an agent decision; re-run before drawing conclusions.", ""]
            continue
        e = r.eval
        seq = compress([f"{c.tool}{'' if c.ok else ' ✗'}" for c in r.trace.calls]) or "none"
        L += [f"- **Expected:** status `{e.expected_status}`, tools "
              f"`{', '.join(e.expected_tools) or '–'}`, sources `{', '.join(e.expected_sources) or '–'}`.",
              f"- **Observed:** outcome `{e.final_outcome}`, status `{e.final_status}`, "
              f"cited `{', '.join(e.cited_sources) or '–'}`.",
              f"- **Tool sequence:** {seq}"]
        if r.categories:
            L.append(f"- **Failed checks:** {', '.join(r.categories)}.")
        if r.signals:
            L.append(f"- **Fragility signals:** {'; '.join(r.signals)}.")
        for f in e.failed_calls[:3]:
            L.append(f"- **Trace:** {cell(f)}")
        for c in e.citation_errors[:2]:
            L.append(f"- **Citation check:** {cell(c)}")
        if e.stopped_by_limit:
            L.append(f"- **Stop reason:** {', '.join(e.stopped_by_limit)}")
        if r.turns:
            L.append(f"- **Final answer:** {cell(r.turns[-1].answer[:300])}")
        L.append("")

    # 5. Threshold evidence
    current = next(row for row in rows if row["current"])
    L += ["## 5. Evidence behind the retrieval threshold", "",
          f"Labeled queries: {sum(q['label'] == 'supported' for q in queries)} from cases with "
          f"expected sources (must retrieve one of them) and "
          f"{sum(q['label'] == 'unsupported' for q in queries)} from unsupported cases (must "
          "retrieve nothing). Queries come from the agent's actual `search_policy` calls, plus "
          "the ticket subject of each supported case as a probe.", "",
          "| Min matched terms | Min coverage | Supported: expected source retrieved | "
          "Unsupported: false accepts |", "|---|---|---|---|"]
    for row in rows:
        mark = " **(current)**" if row["current"] else ""
        L.append(f"| {row['matched']}{mark} | {row['coverage']:.0%} | "
                 f"{pct(row['hit'], row['pos'])} | {pct(row['false_accept'], row['neg'])} |")
    L += ["", f"At the current setting, {pct(current['hit'], current['pos'])} supported "
          f"queries retrieve an expected source and {pct(current['false_accept'], current['neg'])} "
          "unsupported queries are falsely accepted.", "",
          "Per-query detail at the current threshold:", "",
          "| Query | Origin | Label | Accepted (coverage) | Best near miss (coverage) |",
          "|---|---|---|---|---|"]
    for q in queries:
        acc, near = index.search(q["query"], 10)
        L.append(f"| {cell(q['query'])} | {q['origin']} | {q['label']} | "
                 f"{cell([f'{e.source_id} ({e.coverage:.2f})' for e in acc])} | "
                 f"{cell([f'{m.source_id} ({m.coverage:.2f})' for m in near[:1]])} |")
    L.append("")

    # 6. Recommendations
    L += ["## 6. Prioritized recommendations", "",
          "Ordered by the number of affected cases; preventive items fill the list when "
          "fewer than three failure types were observed.", ""]
    for i, (title, text, basis) in enumerate(recommendations(runs, queries, args.repeats), 1):
        L += [f"{i}. **{title}** ({basis}). {text}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def run_config(args, index: PolicyIndex) -> dict:
    return {"model": args.model, "repeats": args.repeats, "git_commit": git_commit(),
            "now": args.now.isoformat() if args.now else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "limits": {"max_steps": args.limits.max_steps,
                       "max_tool_calls": args.limits.max_tool_calls,
                       "max_tool_errors": args.limits.max_tool_errors,
                       "answer_retries": MAX_ANSWER_RETRIES, "history_turns": MAX_HISTORY_TURNS},
            "retrieval": {"stemmer_version": retrieval.STEMMER_VERSION,
                          "min_matched_terms": retrieval.MIN_MATCHED_TERMS,
                          "min_coverage": retrieval.MIN_COVERAGE,
                          "bm25_k1": retrieval.BM25_K1, "bm25_b": retrieval.BM25_B},
            "policy_sources": sorted(index.source_ids)}


def run_all(client, args) -> Path:
    data = load(args.data)
    index = PolicyIndex.from_dir(Path(args.data) / "knowledge_base")
    out_dir = args.out
    previous = load_previous(args, index, data.cases) if (args.resume or args.rescore) else {}
    if previous:
        print(f"Reusing {len(previous)} completed run(s) from the previous results.json.")
    runs, quota_error = [], None
    for case in data.cases:
        for n in range(1, args.repeats + 1):
            if (case.case_id, n) in previous:
                r, how = previous[(case.case_id, n)], "reused"
            elif args.rescore:
                r, how = not_run(case, n, "--rescore makes no API calls"), "skipped"
            elif quota_error:
                r, how = not_run(case, n, "daily token limit reached"), "skipped"
            else:
                r, how = run_case(client, case, n, args, out_dir), "ran"
                if r.quota_exhausted:
                    quota_error = r.error
            status = "error" if r.error else ("pass" if r.eval.passed else "fail")
            print(f"{case.case_id:<32} run {n}: {status:<5} ({how})")
            runs.append(r)
    if quota_error:
        missing = sum(1 for r in runs if r.error)
        print(f"\nGroq daily token limit reached; {missing} run(s) did not complete:\n  {quota_error[:300]}"
              "\nRun again with --resume after the limit resets; completed runs are kept.")
    passes = defaultdict(list)
    for r in runs:
        if r.eval:
            passes[r.case.case_id].append(r.eval.passed)
    for r in runs:
        rate = sum(passes[r.case.case_id]) / len(passes[r.case.case_id]) if passes[r.case.case_id] else 0
        classify(r, args.limits, rate)
    queries = labeled_queries(runs, data.cases, data)
    rows = sweep(index, queries)
    report = out_dir / "evaluation_report.md"
    write_report(report, runs, data.cases, args, index, queries, rows)
    (out_dir / "results.json").write_text(json.dumps({
        "config": run_config(args, index),
        "runs": [{
            "case_id": r.case.case_id, "run": r.run, "error": r.error, "trace": r.trace_path,
            "categories": r.categories, "signals": r.signals,
            "search_queries": [
                {"query": c.input.get("query"),
                 "accepted": [(e["source_id"], e["coverage"]) for e in (c.output or {}).get("evidence", [])],
                 "near_misses": [(m["source_id"], m["coverage"]) for m in (c.output or {}).get("near_misses", [])],
                 "error": c.error}
                for c in r.trace.calls if c.tool == "search_policy"],
            "turns": [t.model_dump() for t in r.turns],
            "evaluation": r.eval.model_dump() if r.eval else None} for r in runs]},
        indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def run_mock(args) -> Path:
    from mock_model import FAULTS, mock_client_factory
    data = load(args.data)
    index = PolicyIndex.from_dir(Path(args.data) / "knowledge_base")
    args.out = args.out / "mock"
    args.resume = args.rescore = False
    if args.mock == "oracle":
        args.model = "mock-oracle"
        report = run_all(mock_client_factory(data, index), args)
        print("\nEvery case should pass. A failing case cannot be passed as written: check its "
              "expected_tools / expected_status / expected_sources against the tools and policies.")
        return report

    # faults: one run per case and fault, then check the evaluator caught each fault
    args.out.mkdir(parents=True, exist_ok=True)
    rows = ["# Harness fault-injection check", "",
            "Each run uses the scripted oracle with one injected mistake. The evaluator must fail "
            "the run and report the expected category.", "",
            "| Case | Fault | Expected category | Reported categories | Caught |", "|---|---|---|---|---|"]
    caught = total = 0
    for fault, expected in FAULTS.items():
        factory = mock_client_factory(data, index, fault)
        for case in data.cases:
            r = run_case(factory, case, 1, args, args.out)
            if r.error:
                cats, ok = [f"error: {r.error[:80]}"], False
            else:
                classify(r, args.limits, 1.0)
                cats = r.categories
                ok = not r.eval.passed and bool(expected & set(cats))
            total += 1
            caught += ok
            print(f"{case.case_id:<32} {fault:<17} {'caught' if ok else 'MISSED'}")
            rows.append(f"| {case.case_id} | {fault} | {' or '.join(sorted(expected))} | "
                        f"{cell(cats)} | {'✓' if ok else '✗'} |")
    rows += ["", f"**{caught}/{total} injected faults caught.** A miss means the evaluator would "
             "let that mistake pass, or the case gives the fault no way to show (for example "
             "`skip_tool` on a case that expects no tools)."]
    path = args.out / "harness_check.md"
    path.write_text("\n".join(rows), encoding="utf-8")
    print(f"\n{caught}/{total} injected faults caught.")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the evaluation and write the report")
    ap.add_argument("--data", type=Path, default=PROJECT_ROOT / "data")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "reports")
    ap.add_argument("--model")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--now", help="fixed clock for on-call lookups, ISO 8601")
    ap.add_argument("--resume", action="store_true",
                    help="reuse completed runs from the last results.json; run only the rest")
    ap.add_argument("--mock", choices=["oracle", "faults"],
                    help="token-free harness test with a scripted model (output in reports/mock/)")
    ap.add_argument("--rescore", action="store_true",
                    help="no API calls: rescore saved runs with the current evaluator")
    ap.add_argument("--max-steps", type=int, default=Limits.max_steps)
    ap.add_argument("--max-tool-calls", type=int, default=Limits.max_tool_calls)
    ap.add_argument("--max-tool-errors", type=int, default=Limits.max_tool_errors)
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    args.model = args.model or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
    args.now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    args.limits = Limits(args.max_steps, args.max_tool_calls, args.max_tool_errors)
    if args.mock:
        report = run_mock(args)
    elif args.rescore:
        report = run_all(None, args)
    else:
        if not os.environ.get("GROQ_API_KEY"):
            sys.exit("Set GROQ_API_KEY (environment variable or .env file).")
        from groq import Groq
        report = run_all(Groq(max_retries=5), args)  # retries cover per-minute limits (429)
    print(f"\nReport written to {report}")


if __name__ == "__main__":
    main()
