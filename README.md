# agentsql-golden-set

A small, runnable kit for validating any natural-language-to-SQL system against a **golden question set**: business questions with expected SQL and independently verified results. It scores candidate SQL by **result equivalence** (not SQL text), runs a **read-only guard**, and **diffs against a previous report to flag regressions** after a model, prompt or config change.

It is vendor-neutral. It only needs the SQL (or executed query) each system produced for each question, so it works against any NL-to-SQL product, an MCP data server, or a raw LLM. Nothing here claims anything about the internals of a specific vendor.

Stdlib only (Python 3.9+; developed on 3.14). No dependencies.

## Run it in 3 commands

```bash
python3 gen_db.py                                   # deterministic sample database -> data/sample.db
python3 golden/build_golden.py                      # re-derives + cross-verifies the 20 golden answers
python3 eval.py candidates/baseline-llm.jsonl       # score a candidates file (JSONL: {"id","sql"})
# after a change, diff against the last good report (exit code 1 on any regression):
python3 eval.py candidates/degraded-llm.jsonl --baseline reports/baseline-llm.json
python3 -m unittest tests.test_eval -v              # the test suite
```

## What is in the box

| Piece | What it does |
|---|---|
| `gen_db.py` | Seeded Indian B2B distribution business (customers, orders, order_items, products, invoices, payments, regions) with deliberate traps: NULL region and NULL discounts, refunds as negative payments, duplicate customer names, soft-deleted customers/products/orders, cancelled orders, UTC timestamps vs IST reporting (payments at 20:00 UTC are *next day* in IST), month/quarter boundary dates, an engineered tie for most orders, a region with no payments (divide-by-zero). |
| `golden/questions.yaml` | 20 questions (6 easy, 9 medium, 5 hard). Each has the expected SQL, the expected result, a second independently written query, and (for all 20) a pure-Python recomputation from raw rows. `build_golden.py` refuses to write the file unless all agree. All relative dates ("last quarter", "last 90 days") are pinned to an as-of date of 2026-10-15. The file is JSON syntax, which is valid YAML 1.2, so it parses without a YAML library. |
| `eval.py` | Result-equivalence scorer. Order-insensitive unless the question demands order; column names ignored (columns match by position); numeric tolerance (per question; default 0.01, ratios 1e-4); NULL equals NULL; int equals float. Guard: single `SELECT`/`WITH` only, no write/DDL keywords, then execution on a `mode=ro` URI connection with `PRAGMA query_only` and a SQLite authorizer that only permits reads, plus a timeout and row cap. Writes a JSON report and diffs against `--baseline`. |
| `gen_candidates.py` | Asks an LLM (via `freeworker.py -m code`) for SQL given only the schema DDL, optional schema notes, and the question text. `--degrade` drops the refund and soft-delete notes. Raw model output is stored in `candidates/*.raw.jsonl`. |
| `MCP_CHECKLIST.md` | One-page integration-validation checklist for an MCP data server deployment. |
| `tests/test_eval.py` | 10 unit tests: equivalence, guard rejection, regression diff. |

## Real results

Candidate SQL came from a free-lane LLM through `freeworker.py -m code` (the router's `code` tier; the model for each call is recorded in `candidates/*.raw.jsonl`, and the tier can route to different backing models between calls). One sample per question, temperature not controlled. Prompt = schema DDL + schema notes + question (`candidates/*.prompt.txt`).

| Run | Prompt | Accuracy | Easy | Medium | Hard |
|---|---|---|---|---|---|
| `baseline-llm` | schema + all notes | **20/20 = 100%** | 6/6 | 9/9 | 5/5 |
| `degraded-llm` | schema + notes **without** the refund and soft-delete notes | **18/20 = 90%** | 6/6 | 9/9 | 3/5 |

### What the regression demo caught

`python3 eval.py candidates/degraded-llm.jsonl --baseline reports/baseline-llm.json` prints `accuracy 100.0% -> 90.0%` and exits 1 with two regressions:

* **Q20 (hard, refunds net against payments): passed before, now FAIL.** Without the note that refunds are negative amounts in `payments.amount`, the model guessed a `p.method = 'refund'` flag (the real value is `'Refund'`, and the real signal is the sign), so the refund filter matched nothing and the query returned 0 rows instead of 18. This is the failure the prompt change was designed to expose.
* **Q19 (hard, month-over-month growth, IST): passed before, now FAIL.** Honest caveat: this one is **not** caused by the dropped notes. The degraded query used the invalid SQLite modifier `'+5 hours 30 minutes'`, which evaluates to NULL, so the answer was NULL. It is ordinary model variance on a single sample, and it shows why a regression report needs a human read: the harness flagged a real wrong answer, but the cause is not the change you made.

Notably, the soft-delete questions did **not** regress: the schema DDL has an `is_deleted` column and most question texts mention "active" or "non-deleted", so the model inferred the filter. That is a finding about this question set, not about the note: add questions whose text does not give the filter away if you want the harness to protect that behaviour.

### Run history, for honesty

1. First baseline attempt: Q11 and Q19 got no model output (the router returned "no lane available" under 4 parallel calls). `gen_candidates.py` now retries the same prompt up to 3 times and uses 2 workers. Nulls score as `error`, never as model judgments.
2. In that first baseline run Q19 failed because the golden question was ambiguous (the model reasonably filtered out soft-deleted orders; the reference summed all payment rows). I fixed the **question text** (Q08 and Q19 now say "sum all payment rows, no filtering on orders or customers"), rebuilt the golden file, and regenerated the baseline from scratch against the final questions. The 100% baseline above is that regenerated run, not a re-score of the first one. Treat 100% as one sample of a small, fairly easy set, not as a general accuracy claim.
3. The degraded run had three freeworker failures on first pass (Q16, Q19, Q20, same lane exhaustion). They were re-asked with the identical degraded prompt via `gen_candidates.py --fill`; the other 17 outputs were kept untouched.

## Limits

* 20 questions on a toy database. The point is the method, not the sample size; a real engagement grows the set per customer and adds every production mistake as a new question.
* Columns are compared by position, so a system that returns the right columns in a different order fails. That is deliberate (it reflects the question's stated output), and easy to relax.
* The keyword guard is intentionally conservative (a column literally named `update` would be rejected); the real enforcement is the read-only connection plus authorizer, and in production the database role itself (see `MCP_CHECKLIST.md`).
