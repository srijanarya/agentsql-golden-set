#!/usr/bin/env python3
"""Score candidate NL-to-SQL output against a golden question set by RESULT equivalence.

  python3 eval.py candidates/x.jsonl [--baseline reports/prev.json] [--out reports/x.json]

Candidates file: JSONL, one {"id": "Q01", "sql": "SELECT ..."} per line.
Works against any NL-to-SQL system: it only needs the SQL (or the executed query) each one produced.
Stdlib only. Exit code 1 if any regression vs the baseline report is found.
"""
import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FORBIDDEN = {"insert", "update", "delete", "drop", "alter", "create", "attach", "detach",
             "pragma", "vacuum", "reindex", "replace", "truncate", "grant", "revoke"}
# 'replace' is only forbidden as a statement keyword; the REPLACE() string function is allowed (see guard()).

# ---------------------------------------------------------------- read-only guard
def _scan(sql):
    """Return (words, error). words = identifier-ish tokens outside strings/comments.
    error is set for unterminated quotes/comments or a second statement after ';'."""
    words, cur, i, n, seen_semi = [], [], 0, len(sql), False
    def flush():
        if cur:
            words.append("".join(cur).lower()); cur.clear()
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            flush(); j = sql.find("\n", i); i = n if j < 0 else j + 1; continue
        if sql.startswith("/*", i):
            flush(); j = sql.find("*/", i + 2)
            if j < 0: return words, "unterminated comment"
            i = j + 2; continue
        if c in "'\"`[":
            flush(); close = "]" if c == "[" else c; j = i + 1
            while True:
                j = sql.find(close, j)
                if j < 0: return words, "unterminated quote"
                if close != "]" and sql.startswith(close, j + 1): j += 2; continue   # doubled quote
                break
            if seen_semi: return words, "multiple statements"
            words.append("<quoted>"); i = j + 1; continue
        if c == ";":
            flush(); seen_semi = True; i += 1; continue
        if c.isspace():
            flush(); i += 1; continue
        if seen_semi: return words, "multiple statements"
        if c.isalnum() or c == "_": cur.append(c)
        else: flush()
        i += 1
    flush()
    return words, None

def guard(sql):
    """Static check: exactly one statement, starting with SELECT or WITH, no write/DDL keywords."""
    if not isinstance(sql, str) or not sql.strip(): return False, "empty SQL"
    words, err = _scan(sql)
    if err: return False, err
    if not words: return False, "empty SQL"
    if words[0] not in ("select", "with"): return False, f"statement must start with SELECT/WITH, got '{words[0]}'"
    for w in words:
        if w in FORBIDDEN and w != "replace": return False, f"forbidden keyword '{w.upper()}'"
    return True, ""

def connect_ro(db):
    """Read-only connection (mode=ro URI) + query_only + an authorizer that only permits reads."""
    con = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    ok = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    con.set_authorizer(lambda code, *a: sqlite3.SQLITE_OK if code in ok else sqlite3.SQLITE_DENY)
    return con

def run_sql(db, sql, timeout_s=5.0, max_rows=5000):
    """Guard + execute read-only. Returns (rows, error). error is 'REJECTED: ...' or 'ERROR: ...'."""
    ok, why = guard(sql)
    if not ok: return None, f"REJECTED: {why}"
    con = connect_ro(db); deadline = time.monotonic() + timeout_s
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        cur = con.execute(sql.strip().rstrip(";"))
        rows = cur.fetchmany(max_rows + 1)
        if len(rows) > max_rows: return None, f"ERROR: more than {max_rows} rows"
        return [tuple(r) for r in rows], None
    except Exception as e:
        return None, f"ERROR: {type(e).__name__}: {e}"
    finally:
        con.close()

# ---------------------------------------------------------------- result equivalence
def _num(v): return isinstance(v, (int, float)) and not isinstance(v, bool)

def _val(v):
    if isinstance(v, bool): return int(v)
    if isinstance(v, str): return v.strip()
    return v

def _close(a, b, abs_tol, rel_tol):
    a, b = _val(a), _val(b)
    if a is None or b is None: return a is None and b is None
    if _num(a) and _num(b): return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))
    return a == b

def _row_close(r1, r2, abs_tol, rel_tol):
    return len(r1) == len(r2) and all(_close(x, y, abs_tol, rel_tol) for x, y in zip(r1, r2))

def results_equivalent(expected, got, ordered=False, abs_tol=0.01, rel_tol=1e-6):
    """Compare by values and column position, never by column name or SQL text.
    Unordered unless `ordered`. Returns (ok, reason)."""
    if len(expected) != len(got): return False, f"row count {len(got)} != expected {len(expected)}"
    if expected and got and len(expected[0]) != len(got[0]):
        return False, f"column count {len(got[0])} != expected {len(expected[0])}"
    if ordered:
        for i, (e, g) in enumerate(zip(expected, got)):
            if not _row_close(e, g, abs_tol, rel_tol): return False, f"row {i} differs: got {g} expected {e}"
        return True, ""
    left = list(got)
    for e in expected:
        for j, g in enumerate(left):
            if _row_close(e, g, abs_tol, rel_tol): del left[j]; break
        else:
            return False, f"expected row {e} not found in result"
    return True, ""

# ---------------------------------------------------------------- scoring / reporting
def load_golden(path):
    with open(path) as f:
        txt = "".join(l for l in f if not l.lstrip().startswith("#"))
    return json.loads(txt)["questions"]       # questions.yaml is JSON (a YAML 1.2 subset): stdlib-parseable

def load_candidates(path):
    out = {}
    with open(path) as f:
        for ln, line in enumerate(f, 1):
            if not line.strip(): continue
            d = json.loads(line)
            qid = d.get("id") or d.get("question_id"); sql = d.get("sql") or d.get("candidate_sql")
            if not qid: raise ValueError(f"{path}:{ln}: missing id")
            out[qid] = sql
    return out

def score(golden, cands, db):
    results = []
    for q in golden:
        r = {"id": q["id"], "difficulty": q["difficulty"], "status": None, "detail": "", "sql": cands.get(q["id"])}
        if q["id"] not in cands:
            r.update(status="missing", detail="no candidate for this question")
        elif not cands[q["id"]]:
            r.update(status="error", detail="generator produced no SQL (null); not a model judgment")
        else:
            rows, err = run_sql(db, cands[q["id"]])
            if err: r.update(status="rejected" if err.startswith("REJECTED") else "error", detail=err)
            else:
                ok, why = results_equivalent(q["expected_result"]["rows"], rows, q.get("ordered", False),
                                             q.get("abs_tol", 0.01))
                r.update(status="pass" if ok else "fail", detail=why)
        results.append(r)
    return results

def summarize(results):
    n = len(results); p = sum(r["status"] == "pass" for r in results)
    by = {}
    for r in results:
        d = by.setdefault(r["difficulty"], {"total": 0, "passed": 0}); d["total"] += 1; d["passed"] += r["status"] == "pass"
    counts = {}
    for r in results: counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"total": n, "passed": p, "accuracy": round(p / n, 4) if n else 0.0, "by_difficulty": by, "status_counts": counts}

def diff_reports(prev, cur):
    pm = {r["id"]: r["status"] for r in prev["results"]}; cm = {r["id"]: r["status"] for r in cur["results"]}
    reg = sorted(i for i in cm if pm.get(i) == "pass" and cm[i] != "pass")
    fix = sorted(i for i in cm if pm.get(i) not in (None, "pass") and cm[i] == "pass")
    return {"regressions": reg, "fixes": fix,
            "accuracy_before": prev["summary"]["accuracy"], "accuracy_after": cur["summary"]["accuracy"]}

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidates"); ap.add_argument("--db", default=os.path.join(HERE, "data", "sample.db"))
    ap.add_argument("--golden", default=os.path.join(HERE, "golden", "questions.yaml"))
    ap.add_argument("--baseline", help="previous report JSON to diff against")
    ap.add_argument("--out", help="report path (default reports/<candidates-stem>.json)")
    a = ap.parse_args(argv)
    golden = load_golden(a.golden); results = score(golden, load_candidates(a.candidates), a.db)
    report = {"candidates": os.path.basename(a.candidates), "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "summary": summarize(results), "results": results}
    out = a.out or os.path.join(HERE, "reports", os.path.splitext(os.path.basename(a.candidates))[0] + ".json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print(f"{'ID':<5}{'DIFF':<8}{'STATUS':<10}DETAIL")
    for r in results: print(f"{r['id']:<5}{r['difficulty']:<8}{r['status'].upper():<10}{r['detail'][:90]}")
    s = report["summary"]
    print(f"\nAccuracy: {s['passed']}/{s['total']} = {s['accuracy']:.1%}   " +
          "  ".join(f"{k}: {v['passed']}/{v['total']}" for k, v in s["by_difficulty"].items()) + f"   statuses: {s['status_counts']}")
    rc = 0
    if a.baseline:
        with open(a.baseline) as bf: prev = json.load(bf)
        d = diff_reports(prev, report); report["diff_vs_baseline"] = {**d, "baseline": os.path.basename(a.baseline)}
        print(f"\nvs baseline {os.path.basename(a.baseline)}: accuracy {d['accuracy_before']:.1%} -> {d['accuracy_after']:.1%}")
        for i in d["regressions"]: print(f"  REGRESSION {i}: passed before, now {next(r for r in results if r['id']==i)['status'].upper()}")
        for i in d["fixes"]: print(f"  fixed      {i}")
        if not d["regressions"]: print("  no regressions")
        rc = 1 if d["regressions"] else 0
    with open(out, "w") as of: json.dump(report, of, indent=2)
    print(f"report: {out}")
    return rc

if __name__ == "__main__":
    sys.exit(main())
