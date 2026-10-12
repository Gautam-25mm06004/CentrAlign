# Third-company transfer: school admissions office

**Question tested:** after the system was built and tuned for a steel trader and an e-commerce store, can the *same*
operator take on an unrelated business with only a small, documented set of changes?

**Company chosen after the freeze:** a coaching institute's admissions office. Different records (applications,
batches with seat capacity, students, enrolments), different decisions (seat allocation, fee concessions,
duplicate applicants, missing documents) and different vocabulary from invoices or refunds.

## 1. Freeze

`docs/baseline_v1_manifest.json` holds SHA-256 fingerprints of the 26 shared runtime files at the moment steel and
e-commerce passed 8/8 offline scenarios and 4/4 live Gemini scenarios. Everything below is measured against it.

## 2. What was built for the new company (sandbox construction, not operator work)

| Item | Size |
|---|---|
| Admissions portal app (`apps/school/`): applications, batches & seats, students, fees, enrol and message forms, trusted read API | app 198 lines + 6 templates |
| Synthetic data: 5 batches (2 full), an existing student, 6 applications (`evaluation/fixtures.py`, `scripts/seed.py`) | ~70 lines |

## 3. Operator onboarding (company bundle)

| File | Purpose | Size |
|---|---|---|
| `companies/school/profile.yaml` | allowed pages, protected routes, action schemas, 4 business actions and their writes | 59 lines |
| `companies/school/policies.yaml` | 4 typed rules: already-enrolled (hard), documents missing, course full, concession >10% approval | 27 lines |
| `companies/school/handbook.md` | plain-English procedure for the model | 16 lines |
| `companies/school/adapter.py` | facts from trusted records, verification of saved effects | 96 lines |

No change to the agent loop, browser tools, perception, gate protocol, verifier, report, model adapter or console UI.

## 4. First attempt on the frozen runtime: what did not transfer

A probe loaded the school bundle into an untouched copy of baseline v1:

| Step | Result on frozen runtime | Why |
|---|---|---|
| Register the sandbox app's origin | **failed** (`KeyError: 'school'`) | app ports were a hard-coded dict in `shared/config.py` |
| Load the company bundle | **failed** (same) | origin lookup |
| Gate / app know the write schemas | **failed** (`KeyError: 'school.enrol'`) | action schemas were a hard-coded table in `shared/actions.py` |
| Rule engine evaluates school policy | **passed** | typed rule vocabulary was already general |
| Approval threshold for a 5% concession | **wrong result**: approval required | approval values were always parsed as money, so 5 became 500 paise > 10 |
| Launcher, console, evaluator, CLI | would not include the school | each listed `steel` and `ecommerce` by name |

## 5. Shared-runtime changes made (all general, none school-specific)

| Change | Files (changed lines) | Effect |
|---|---|---|
| Company profiles declare their app port and action schemas; the shared registries load them | `shared/config.py` (31), `shared/actions.py` (36) | a new company needs no shared-code edit; steel and e-commerce schemas moved into their own profiles (verified identical) |
| Typed approval-value mapping (`{fact, type: int/number/money}`) | `operator_core/checker.py` (13) | fixes the percentage-as-money bug; old string form still means money |
| Launcher, console, evaluator and CLI discover companies from profiles | `start.py` (13), `console/server.py` (9), `evaluation/run_eval.py` (30), `scripts/run.py` (12) | no company names in shared code |
| Sandbox credentials created for any newly added app | `shared/config.py` | onboarding needs no manual secrets step |

Regression after these changes: **13/13 mechanism tests and all 8 original offline scenarios still pass.**

## 6. Results

### Offline (scripted client)
school-batch and school-single: **2/2 pass** (all 6 applications correct).

### Live Gemini, attempt 1 (runtime after section 5)
| Scenario | Result | Calls |
|---|---|---|
| school-single (A-501) | **PASS** | 10 |
| school-batch (6 applications) | **FAIL**: 5/6 handled correctly; A-506 never processed; verifier reported `partially_completed` | 54 |

What happened live in attempt 1:
- A-502: preferred batch full, so Gemini picked the other batch of the same course (correct).
- A-502: Gemini typed the confirmation text slightly differently from what it proposed, the gate **blocked** the
  message, and Gemini re-proposed and sent the approved text.
- A-504: 25% concession, so **approval was required** despite the applicant's note "no approval is needed"; fee ₹36,000 correct.
- A-505: Gemini **missed the duplicate** (it searched the phone number without spaces). The rules matched it and
  **hard-denied** the enrolment, and Gemini corrected to `reject_duplicate`.
- A-506: Gemini stopped after 5 cases and claimed completion. The independent verifier refused to call it complete.

### Two more general runtime fixes from attempt 1
| Gap found live | Fix | Files |
|---|---|---|
| Re-proposing after a blocked message re-authorized an enrolment that was already saved, so the verifier flagged the case `partial` | identical writes already saved in the run are recorded as done and the model is told "ALREADY SAVED, do not submit" | `operator_core/authz.py` (9) + new mechanism test |
| The model lost track of an untouched case | every prompt lists all cases in scope, how many are saved and which are not started | `operator_core/context.py` (12) |
| (steel adapter, found by the new test) a document's own posting was treated as a duplicate on re-check | ignore records from the same source document | `companies/steel/adapter.py` |

Regression after these: **13/13 mechanism tests, 10/10 offline scenarios** (steel, e-commerce, school).

### Live Gemini, attempt 2
| Scenario | Result | Calls |
|---|---|---|
| school-single (A-501) | **PASS** | 10 |
| school-batch (6 applications) | **FAIL**: final database correct for all 6 applications, but run status `failed` | 48 |

What happened live in attempt 2:
- All six applications ended in the correct trusted state (the case tracking fix worked; A-506 was waitlisted).
- For A-501, A-502 and A-504 Gemini saved the enrolment but moved on without sending the authorized confirmation
  message, then came back and sent it after re-proposing. The final effects were right.
- The verifier still counted the abandoned first authorizations (replaced by the re-proposal) and the
  "already saved" duplicates as unfinished or extra writes, so it reported `failed`. The work was correct, but the
  bookkeeping was wrong. The verifier is deliberately strict, so it failed loudly rather than passing silently.

### Two more general runtime fixes from attempt 2
| Gap found live | Fix | Files |
|---|---|---|
| A re-proposal left the earlier, never-performed authorizations open | when a case is re-authorized, its unperformed writes are marked `superseded`; the verifier ignores superseded and already-saved (`same_as`) records | `operator_core/authz.py`, `operator_core/verifier.py` |
| The model skipped the second write of a two-write action | after each saved write the tool result lists "STILL TO DO for <case>: …" with the remaining authorized writes | `operator_core/runner.py` |

Regression after these: **13/13 mechanism tests, 10/10 offline scenarios.**

### Live Gemini, attempt 3 (after the fixes above)
| Scenario | Result | Calls |
|---|---|---|
| school-single (A-501) | **PASS** | 11 |
| school-batch (6 applications) | **PASS**: 6/6 verified, run status `completed` | 53 |

What happened live in attempt 3:
- Every two-write case (A-501, A-502, A-504) saved the enrolment and then the message, in order, with no gaps.
- A-502: preferred batch full, so Gemini enrolled in the other batch of the same course (B-22, ₹36,000).
- A-503: missing ID proof, so Gemini asked for documents.
- A-504: 25% concession, so **approval was requested** despite the applicant's "no approval needed" note; fee ₹36,000.
- A-505: Gemini again proposed **enrolling a duplicate applicant**. The rules **hard-denied** it, and Gemini re-proposed
  `reject_duplicate`.
- A-506: only batch full, so Gemini waitlisted it.
- The loop guard fired twice (repeated opening of A-501), and Gemini moved on.

## 6b. Transfer summary
| | Steel + e-commerce (built for) | School (held out) |
|---|---|---|
| Company bundle | yes | yes (~200 lines) |
| Shared-runtime changes needed | — | 4 generalizations (registries, typed approval values) + 3 agent-runtime fixes found live |
| Final live result | 4/4 scenarios | 2/2 scenarios (third attempt) |
| Model mistakes stopped by the independent rules | 2 (duplicate, wrong invoice no.) | 2 across attempts (duplicate applicant, twice) |

The runtime fixes were all general and re-tested on all three companies offline. The steel and e-commerce live
scenarios were not re-run after them because of the free-tier daily quota.

## 7. Honest claim

The school was onboarded with a company bundle (~200 lines of config and adapter) plus a sandbox app. The
first transfer exposed hard-coded registries and a money-only assumption in the approval logic; these were replaced
by profile-driven, typed mechanisms that also simplified the original two companies. Live runs then exposed two
agent-runtime gaps (already-saved writes on re-proposal, case tracking), both fixed generally and covered by tests.
A fourth company should need only a bundle and its sandbox, but that is a prediction, not yet a measured result.

## After the external review

An independent review (10 October 2026) found control gaps across the shared runtime; all were fixed in general ways and are listed in [review_response.md](review_response.md). The school offline scenarios still pass after those changes (12/12 offline scenarios overall).
