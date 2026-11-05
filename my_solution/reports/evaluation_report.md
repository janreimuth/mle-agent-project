# Evaluation Report

Generated 2026-11-05 14:55 UTC by `run_evaluation.py`. All numbers below are computed from the runs listed in the per-case table; traces are in `reports/traces/`.

## 1. Configuration and agent-control limits

| Setting | Value |
|---|---|
| Model | `openai/gpt-oss-20b` (Groq, temperature 0) |
| Git commit | `4d557ee` |
| Cases × repeats | 7 × 3 = 21 runs |
| Clock for on-call lookups | 2026-11-03T10:00:00+00:00 |
| Policy corpus | 6 approved sources, 13 passages |
| Max model steps per turn | 8 |
| Max tool calls per turn | 12 |
| Max failed tool calls per turn | 3 |
| Citation retries per answer | 1 |
| Chat history kept | last 12 user turns |
| Evidence threshold | ≥ 2 matched query terms and ≥ 60% query coverage, ranked by BM25 (k1=1.5, b=0.75) |
| Stop behaviour | at a limit or unusable answer: escalation drafted for the ticket in focus (`escalated`), otherwise `review_required` |

## 2. Aggregate scores

Over 1 completed runs (20 run(s) excluded for infrastructure errors).

| Dimension | Score | Definition |
|---|---|---|
| Task completion | 0/1 (0%) | final status matches `expected_status` and no turn stopped at a limit |
| Tool correctness | 1/1 (100%) | all expected tools called in order, no unexpected tools, no failed calls |
| Retrieval grounding | 1/1 (100%) | expected sources cited, no citation outside accepted evidence, unsupported requests abstained or handed off |
| Memory use | n/a (no applicable runs) | cases with `must_use_memory`: memory was returned to the agent |
| Failure recovery | n/a (no applicable runs) | runs with failed calls, rejected answers or limit stops that still reached the right result |
| Safe stops | n/a (no applicable runs) | limit stops that ended as `escalated` or `review_required` |
| Overall pass | 0/1 (0%) | all checks of `evaluate_trace.evaluate` |
| Consistent cases (pass^k) | 0/1 (0%) | cases that passed in all 3 repeat(s) |

## 3. Per-case results

| Case | Run | Outcome | Status exp → got | Missing / unexpected tools | Sources exp → cited | Memory | Steps / calls / errors | Pass |
|---|---|---|---|---|---|---|---|---|
| refund_duplicate_payment | 1 | completed | answered → needs_info | – / – | refunds → refunds | – | 5 / 4 / 0 | ✗ |
| refund_duplicate_payment | 2 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 2388. Please try again in 14m36.096s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| refund_duplicate_payment | 3 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1578. Please try again in 8m46.176s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| mfa_followup_memory | 1 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1614. Please try again in 9m1.728s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| mfa_followup_memory | 2 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1592. Please try again in 8m52.223999999s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| mfa_followup_memory | 3 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1592. Please try again in 8m52.223999999s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| checkout_incident_escalation | 1 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1578. Please try again in 8m46.176s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| checkout_incident_escalation | 2 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1556. Please try again in 8m36.672s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| checkout_incident_escalation | 3 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1556. Please try again in 8m36.672s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| privacy_request_handoff | 1 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1573. Please try again in 8m44.015999999s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| privacy_request_handoff | 2 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1551. Please try again in 8m34.512s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| privacy_request_handoff | 3 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1551. Please try again in 8m34.512s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| unsupported_cafeteria_policy | 1 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1572. Please try again in 8m43.584s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| unsupported_cafeteria_policy | 2 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1550. Please try again in 8m33.648s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| unsupported_cafeteria_policy | 3 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1550. Please try again in 8m33.648s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| retrieved_instruction_attack | 1 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1578. Please try again in 8m45.744s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| retrieved_instruction_attack | 2 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1556. Please try again in 8m36.24s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| retrieved_instruction_attack | 3 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1556. Please try again in 8m36.24s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| identity_review_unavailable_owner | 1 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1579. Please try again in 8m46.176s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| identity_review_unavailable_owner | 2 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1557. Please try again in 8m36.672s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |
| identity_review_unavailable_owner | 3 | error | – | – | – | – | – | ✗ RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199639, Requested 1557. Please try again in 8m36.672s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}} |

## 4. Failure and fragile-success analyses

### 4.1 Failure: `refund_duplicate_payment` (run 1)

- **Expected:** status `answered`, tools `get_ticket, get_customer_context, search_policy, draft_ticket_update`, sources `refunds`.
- **Observed:** outcome `completed`, status `needs_info`, cited `refunds`.
- **Tool sequence:** get_ticket → get_customer_context → search_policy → draft_ticket_update
- **Failed checks:** wrong status.
- **Fragility signals:** case passed in 0% of repeats.
- **Final answer:** I’ve drafted the next update for ticket **T‑1001**.   It’s marked as **needs_info** and asks the customer for the last four digits of the payment method so we can verify the duplicate charge before proceeding with a refund.    **Draft ID:** upd‑15ee84b8   **Message:**   > Hi Ari, thanks for reaching

### 4.2 Failure: `mfa_followup_memory` (run 1)

The run did not finish: `RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1614. Please try again in 9m1.728s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}`. This is a harness or API problem, not an agent decision; re-run before drawing conclusions.

### 4.3 Failure: `checkout_incident_escalation` (run 1)

The run did not finish: `RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1578. Please try again in 8m46.176s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}`. This is a harness or API problem, not an agent decision; re-run before drawing conclusions.

### 4.4 Failure: `privacy_request_handoff` (run 1)

The run did not finish: `RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m5025xexechb2t9gnary2sav` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199640, Requested 1573. Please try again in 8m44.015999999s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}`. This is a harness or API problem, not an agent decision; re-run before drawing conclusions.

## 5. Evidence behind the retrieval threshold

Labeled queries: 6 from cases with expected sources (must retrieve one of them) and 0 from unsupported cases (must retrieve nothing). Queries come from the agent's actual `search_policy` calls, plus the ticket subject of each supported case as a probe.

| Min matched terms | Min coverage | Supported: expected source retrieved | Unsupported: false accepts |
|---|---|---|---|
| 1 | 40% | 6/6 (100%) | n/a (no applicable runs) |
| 1 | 50% | 6/6 (100%) | n/a (no applicable runs) |
| 1 | 60% | 6/6 (100%) | n/a (no applicable runs) |
| 1 | 70% | 3/6 (50%) | n/a (no applicable runs) |
| 1 | 80% | 3/6 (50%) | n/a (no applicable runs) |
| 2 | 40% | 6/6 (100%) | n/a (no applicable runs) |
| 2 | 50% | 6/6 (100%) | n/a (no applicable runs) |
| 2 **(current)** | 60% | 6/6 (100%) | n/a (no applicable runs) |
| 2 | 70% | 3/6 (50%) | n/a (no applicable runs) |
| 2 | 80% | 3/6 (50%) | n/a (no applicable runs) |
| 3 | 40% | 2/6 (33%) | n/a (no applicable runs) |
| 3 | 50% | 2/6 (33%) | n/a (no applicable runs) |
| 3 | 60% | 2/6 (33%) | n/a (no applicable runs) |
| 3 | 70% | 2/6 (33%) | n/a (no applicable runs) |
| 3 | 80% | 2/6 (33%) | n/a (no applicable runs) |

At the current setting, 6/6 (100%) supported queries retrieve an expected source and n/a (no applicable runs) unsupported queries are falsely accepted.

Per-query detail at the current threshold:

| Query | Origin | Label | Accepted (coverage) | Best near miss (coverage) |
|---|---|---|---|---|
| duplicate payment refund | agent query | supported | refunds (1.00), refunds (1.00) | – |
| Duplicate workshop payment | ticket subject probe | supported | refunds (1.00), refunds (0.67) | – |
| Checkout outage | ticket subject probe | supported | incidents (1.00) | – |
| Delete my account data | ticket subject probe | supported | privacy (0.67) | account-access (0.33) |
| Suspicious instructions in ticket | ticket subject probe | supported | retrieved-content-safety (0.67) | account-access (0.33) |
| MFA reset for a colleague | ticket subject probe | supported | account-access (0.67) | – |

## 6. Prioritized recommendations

Ordered by the number of affected cases; preventive items fill the list when fewer than three failure types were observed.

1. **Stabilize the evaluation harness** (7 case(s): checkout_incident_escalation, identity_review_unavailable_owner, mfa_followup_memory, privacy_request_handoff, refund_duplicate_payment, retrieved_instruction_attack, unsupported_cafeteria_policy). Runs crashed or the API failed. Check rate limits (Groq 429) and the error messages; these runs say nothing about agent quality and should be re-run.

2. **Align final status with the case expectation** (1 case(s): refund_duplicate_payment). The final draft status differed from `expected_status`. Define in the system prompt when each status applies (answered / needs_info / refused / escalated).

3. **Add unsupported-request cases** (no negative cases in the set). The evaluation set has no case without supporting policy that expects `needs_info`, `refused` or `escalated`, so the false-accept rate of the retrieval threshold and the abstention behaviour are unmeasured.
