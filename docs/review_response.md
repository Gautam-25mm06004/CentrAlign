# Response to the external review (D1–D10, R1–R6)

The review (10 October 2026) found correctness and control gaps that the earlier tests did not cover: the existing
tests proved that the scenarios work, not that the controls hold in every situation. Every finding below was
reproduced as an automated test against the real sandbox apps, gate, browser and verifier, then fixed. To show the
tests detect the problem, each fix was temporarily removed and the matching test failed (see "Mutation check").

**Result:** 13/13 original mechanism tests, **26/26 review regression tests** (`tests/test_review_findings.py`) and
**12/12 offline scenarios** (8 steel + e-commerce, 2 school, 2 new task-limit scenarios) pass.

## New findings

| ID | Finding | Fix | Where | Test |
|---|---|---|---|---|
| D1 | Wrong grade / subtotal / tax were not compared; inconsistent values could be posted and verified | Grade (case-insensitive "label" type), subtotal and tax are now compared with the independently extracted invoice. Proposed values must be internally consistent (qty × rate, 18% GST, total) before any authorization, even if a human follows the agent. The accounts app itself refuses postings that do not reconcile or do not match the PO's grade, rate and quantity tolerance, inside the save transaction. | `companies/steel/profile.yaml`, `companies/steel/adapter.py` (`check_values`), `operator_core/authz.py`, `apps/steel/app.py` | `D1MaterialInvoiceFields` (9 single-field changes, consistency, app refusal) |
| D2 | A human stop left earlier authorizations usable | Stops are persisted (`case_controls` table), every outstanding permission for the case is revoked, the request guard re-checks stop state at the write boundary, and a new model proposal can never reopen a stopped case. Reopening is a separate recorded human action in the console (with a reason). Approval rejection stops the case the same way. | `operator_core/store.py`, `operator_core/authz.py`, `console/server.py` | `D2StopRevokes` (incl. restart and approval rejection) |
| D3 | Cancellation during a model call still let its returned write through | Cancellation is checked after every model call (returned actions are discarded and recorded), before every tool, inside `propose`, and in the request guard before any protected request leaves the browser. On cancel, unused permissions are revoked and anything already dispatched is reconciled and reported. | `operator_core/runner.py`, `operator_core/authz.py` | `D3Cancellation`, `D3CancelDuringModelCall` |
| D4 | Authorizations survived changes that invalidated the decision | Every authorization stores its decision basis (the checker's facts, expected values and a policy hash). Immediately before the decision's first effect the guard re-reads the records; any change marks the permission stale and requires a new decision (and a new approval where needed). The accounts app also re-checks the PO in the transaction. | `operator_core/authz.py` (`basis_changes`), `operator_core/store.py`, `apps/steel/app.py` | `D4StaleAuthorization` |
| D5 | Artifact downloads could escape the run directory | The run id must match `run-<8 hex>` and exist; its folder must be a direct child of `runs/`; the file must resolve inside that folder (after decoding and symlink resolution). Two independent layers; removing both lets the test fail. | `console/server.py` | `D5ArtifactBoundary` (real HTTP server) |
| D6 | Restart did not reconcile submitted writes | A recovery phase runs before the model acts again: every submitted/uncertain write is reconciled through the trusted idempotency record (a failed lookup stays unknown), human decisions the dead worker was waiting for are closed as interrupted, stopped cases are restored from storage, and the model is told the recovered state. Active run time is persisted across restarts. | `operator_core/runner.py` (`_recover`), `operator_core/authz.py` (`reconcile_case`) | `D6RestartRecovery` (controlled checkpoint test, as in the review) |
| D7 | Verification missed quantity, grade, identity links and replacement details | Steel: quantity, grade, source document and posted arithmetic. School: enrolment linked to a new student record with the applicant's name/phone/email, batch of the right course, capacity; stray student records reported. E-commerce: replacement line, product and quantity, stock reduced by exactly that quantity; stock changes without replacements reported. | `companies/*/adapter.py` | `D7VerificationMutations`, `D7SchoolLink`, `D7Replacement` |
| D8 | Rounding moved the seven-day boundary | Elapsed time is compared exactly (seconds / 86400); 7 days is eligible, 7 days + 1 s is not. A request dated before delivery goes to clarification. | `companies/ecommerce/adapter.py`, `companies/ecommerce/policies.yaml` | `Units.test_d8_return_window_exact_seconds` |
| D9 | Numeric parsing silently changed meanings | Explicit grammars: amounts (optional ₹/Rs/INR, digit grouping, ≤ 2 decimals, non-negative), quantities (non-negative, ≤ 3 decimals), whole numbers (optional %). `-6.000`, `1.5` as an integer, `1e3`, `12abc` and `1.234` are rejected with a clear error. No float arithmetic for money. | `shared/actions.py` | `Units.test_d9_parsing_rejects_reinterpretation` |
| D10 | Limits captured at import time; the context ceiling covered only part of the prompt | `.env` is loaded before any runtime module in every launcher, and limits are read at run time. The ceiling now bounds the whole request (system prompt + tool declarations + user prompt). Every pinned section is capped; history, then old facts, then the observation are narrowed; if it still cannot fit, no request is sent and the run fails with the reason. | `scripts/run.py`, `evaluation/run_eval.py`, `console/server.py`, `operator_core/runner.py`, `operator_core/context.py` | `Units.test_d10_limits_read_at_run_time_and_full_request_ceiling` |

## Earlier findings

| ID | Finding | Fix | Test |
|---|---|---|---|
| R1 | Rules fallback was removed | After free capacity ends, no further model calls are made. A labelled **rules executor** completes every case the rules fully cover, through the same gate (source `rules`), human approval, exact authorization, the application's own forms in the same controlled browser, signed capability, app validation, idempotency and verifier. Cases the rules cannot decide are queued for a human as *not executed*; the run waits as `waiting_for_quota` and can resume with Gemini. `QUOTA_FALLBACK=none` disables it. (`operator_core/fallback.py`) | `R1RulesExecutorAfterQuota` (6 steel invoices incl. an approval), `R1UncoveredCasesGoToHuman` |
| R2 | Partial cases disappeared from quota recommendations | Remaining work is decided by the verifier, not by "has any write". Unfinished obligations of an accepted decision (e.g. a saved refund with its reply outstanding) are completed by the executor or listed for the human. | `R2PartialCaseFinishedAfterQuota` |
| R3 | Reply could precede refund; an expired refund could be reported completed | The guard enforces write order within a decision. The verifier requires every write of the current accepted decision to be saved; expired, rejected, stale or revoked obligations keep the case incomplete. | `R3Sequencing` |
| R4 | Narrow user scope not enforced; read-only tasks could write | Cases named in the request limit the scope to exactly those; a named case that is not open gives an empty scope instead of widening it. Information-only requests ("don't change anything", "just tell me", "dry run", or the console's Read-only checkbox / `--read-only`) are read-only runs: decisions are stored as recommendations, nothing can be authorized, the guard blocks every write, and verification requires a recommendation per case and zero changes. (`operator_core/taskscope.py`) | `R4NarrowScope`, `R4UnknownCaseNeverWidensScope`, `R4ReadOnly`; scenarios `ecom-narrow-scope`, `ecom-read-only` |
| R5 | Failed reconciliation was treated as proof of absence | A failed lookup keeps the write `uncertain`; the model is told the outcome is unknown; any new decision for that case is blocked until reconciliation succeeds. Resubmitting the same values stays safe (same idempotency key). | `R5UnknownOutcome` |
| R6 | Quota limits and accounting were inconsistent | An evaluation suite's allocation is enforced per attempt (each run's cap is what remains). Scripted calls never count toward free-tier pacing or the daily allocation. The setup check is metered in the ledger. If `GEMINI_RPD` is not set, a warning is shown in the console, the CLI, the setup check and the run's events instead of silently disabling protection. | `Units.test_r6_per_run_cap_and_scripted_calls_not_counted_as_quota` |

## Live re-check with Gemini

After the fixes, `live-after-review` was run live on the laptop (free-tier `gemini-3.5-flash-lite`, 11 Oct 2026):
**4/4 PASS, 33 model calls.** steel-single (9 calls) and school-single (10) completed with every check; the request
"Resolve ticket T-301 only." was limited to T-301 and nothing else changed (8 calls); the request "…tell me what
should happen to each. Do not change anything." ran read-only, recorded two recommendations and changed nothing
(6 calls). Reports: `docs/evidence/live-after-review/`.

## Mutation check

Each control was removed in turn and the relevant test was run. Every removal was detected. Three controls are
deliberately layered, so removing one alone is still caught by the other layer:

- file downloads: run-id format/existence and the parent-folder check (removing both is detected);
- cancellation: the check after the model call, the check before each tool, and the request guard (the test asserts
  the returned actions are discarded before any of them runs);
- replacement verification: the saved quantity and the stock change both detect a wrong quantity.

## What remains open

- Restart recovery is tested with a controlled checkpoint (a stored `submitted` write and a pending decision), as in
  the review, not with an operating-system process kill.
- Token counts in the ceiling are a local estimate (characters / 4), reported as an estimate.
- Read-only detection from wording is a heuristic; the explicit checkbox / flag is the reliable way. Wording it does
  not recognise is treated as a normal task (scope and every other control still apply).
- The decision basis covers the facts the rules use; a change to a record field no rule depends on does not
  invalidate a permission (by design).
- Daily protection still depends on `GEMINI_RPD` being set; it is no longer silent when missing.

## Second review (11 October 2026)

A second independent review reproduced four behaviour gaps outside the existing fixtures and a few presentation
issues. All were confirmed against the code, fixed within the existing modules (no new services, libraries or
frameworks), covered by `tests/test_second_review.py` (16 tests) and mutation-checked: removing each fix makes its
test fail. Afterwards: **55/55 tests and 12/12 offline scenarios pass.** No Gemini calls were made for this round.

| Finding | Fix | Where |
|---|---|---|
| "All tickets except T-301" selected only T-301; "excluding T-301 and T-302" selected those two | A small, documented request grammar: all open cases, named cases, all except named cases. Ids after an exclusion word are excluded; a case both requested and excluded is an issue | `operator_core/taskscope.py` |
| Read-only wording missed "without making edits" / "just prepare recommendations"; "do not change customer contact details" made a whole writable task read-only | Clear information-only wording means read-only. A partial restriction or an amount/date filter is reported as unenforceable: before any write the run asks a person to proceed with the listed cases (reason required), switch to recommendations only, or stop. The automatic test operator chooses recommendations only. Never silently writable | `taskscope.py`, `operator_core/runner.py` (`_confirm_scope`, restart-safe), `console/` (scope card) |
| An unknown case (T-999) gave an empty scope that verified as completed | A missing requested case is stored as unresolved: with nothing valid to do the run ends as `needs_clarification` with no model calls; otherwise the run can be at most `partially_completed`. A valid empty queue finishes as completed with "No pending work was found." | `runner.py`, `operator_core/verifier.py` |
| Two replacements of the same product failed verification (each compared its own quantity with the shared stock change) | Each ticket's replacement record is checked against its own authorized line, product and quantity; the product's stock must fall by the total of all new replacements of it | `companies/ecommerce/adapter.py` |
| A read-only run could pass with a changed ticket status | Read-only verification also compares every business record in the trusted before/after snapshots (added, deleted or changed) | `verifier.py` (`diff_records`) |
| "Initial proposals" counted re-proposals | The report shows first-attempt agreement (earliest proposal per case) and, separately, all proposals including re-proposals | `console/static/app.js`, `operator_core/report.py` |
| Setup printed success after a failed step | `1_setup.bat` stops with a clear message at the failing step; the key check exits non-zero on failure | `1_setup.bat`, `scripts/gemini_check.py` |
| Unpinned dependencies | Versions constrained to the two tested environments (Windows/Python 3.14 live, Linux/Python 3.13 offline) | `requirements.txt` |
| Transfer claim | README states the school needed the bundle plus 4 shared generalisations and 3 runtime fixes; a zero-edit fourth company is untested | `README.md` |

Still open: the scope grammar is intentionally small (not general language understanding); the record comparison
for read-only runs assumes a single-worker sandbox (a shared system would need per-change attribution); the
changes in this round have not yet been exercised by a fresh live Gemini run.
