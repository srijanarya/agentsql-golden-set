#!/usr/bin/env python3
"""Generate candidate SQL for every golden question with an LLM via the free-first freeworker,
giving it ONLY the schema (+ optional schema notes) and the question text.

  python3 gen_candidates.py baseline-llm            # full schema notes
  python3 gen_candidates.py degraded-llm --degrade  # drops the refund + soft-delete notes

Writes candidates/<name>.jsonl and candidates/<name>.prompt.txt (the exact prompt template) so the
run is reproducible/auditable. Raw model output is kept in candidates/<name>.raw.jsonl. Never fabricates output:
if freeworker fails for a question the line is written with sql=null and the question scores 'error'.
"""
import argparse, json, os, re, sqlite3, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__))
FREEWORKER = os.environ.get("SQL_LLM_SCRIPT", os.path.expanduser("~/.agents/skills/free-first/freeworker.py"))  # any script that takes a prompt and prints SQL
sys.path.insert(0, HERE)
from eval import load_golden

NOTES_CORE = ["Dates are ISO-8601 TEXT (YYYY-MM-DD); payments.paid_at_utc is a UTC timestamp text. The business reports in IST (UTC+05:30).",
              "order_items.discount_pct is a percentage and may be NULL (NULL means no discount).",
              "orders.status is 'completed' or 'cancelled'. invoices.status is 'issued', 'paid' or 'void'."]
NOTES_DEGRADABLE = ["Refunds are stored as NEGATIVE amounts in payments.amount (positive amounts are customer payments).",
                    "Soft deletion: customers, products and orders have is_deleted (1 = deleted). Deleted rows must be excluded from business answers."]

def schema_ddl(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ddl = "\n".join(r[0] + ";" for r in con.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"))
    con.close(); return ddl

def build_prompt(ddl, notes, question):
    n = ("Schema notes:\n" + "\n".join("- " + x for x in notes) + "\n\n") if notes else ""
    return ("You translate business questions into a single read-only SQLite SELECT query.\n\n"
            f"Database schema:\n{ddl}\n\n{n}Question: {question}\n\n"
            "Return ONLY the SQL query (one statement, no explanation, no markdown fences).")

def extract_sql(text):
    lines = [l for l in text.splitlines() if not l.startswith("[freeworker]")]
    t = "\n".join(lines).strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", t, re.S | re.I)
    return (m.group(1) if m else t).strip()

def call(prompt):
    # freeworker sometimes exits 1 under lane contention ("no lane available"); retry the SAME prompt up to 3 times.
    for attempt in range(3):
        p = subprocess.run([sys.executable, FREEWORKER, "-m", "code", prompt], capture_output=True, text=True, timeout=300)
        if p.returncode == 0 and p.stdout.strip(): break
        time.sleep(5)
    return p.returncode, p.stdout, p.stderr

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("name"); ap.add_argument("--degrade", action="store_true")
    ap.add_argument("--fill", action="store_true", help="only re-ask questions whose existing candidate is null (same prompt)")
    a = ap.parse_args()
    db = os.path.join(HERE, "data", "sample.db"); ddl = schema_ddl(db)
    notes = NOTES_CORE if a.degrade else NOTES_CORE + NOTES_DEGRADABLE
    qs = load_golden(os.path.join(HERE, "golden", "questions.yaml"))
    prompts = {q["id"]: build_prompt(ddl, notes, q["question"]) for q in qs}
    prev = {}
    if a.fill:
        with open(os.path.join(HERE, "candidates", a.name + ".jsonl")) as pf: prev = {json.loads(l)["id"]: json.loads(l)["sql"] for l in pf}
        with open(os.path.join(HERE, "candidates", a.name + ".raw.jsonl")) as pr: prevraw = {json.loads(l)["id"]: json.loads(l) for l in pr}
        prompts = {k: v for k, v in prompts.items() if not prev.get(k)}
    with ThreadPoolExecutor(2) as ex: outs = dict(zip(prompts, ex.map(call, prompts.values())))
    out = os.path.join(HERE, "candidates")
    with open(os.path.join(out, a.name + ".jsonl"), "w") as f, open(os.path.join(out, a.name + ".raw.jsonl"), "w") as raw:
        for q in qs:
            if q["id"] not in outs:   # --fill: keep the earlier successful output untouched
                f.write(json.dumps({"id": q["id"], "sql": prev[q["id"]]}) + "\n"); raw.write(json.dumps(prevraw[q["id"]]) + "\n"); continue
            rc, so, se = outs[q["id"]]
            sql = extract_sql(so) if rc == 0 and so.strip() else None
            model = next((l for l in (se + so).splitlines() if "model " in l), "")
            f.write(json.dumps({"id": q["id"], "sql": sql}) + "\n")
            raw.write(json.dumps({"id": q["id"], "exit": rc, "model_line": model.strip(), "stdout": so, "stderr": se[-300:]}) + "\n")
            print(q["id"], "exit", rc, "|", (sql or "NO OUTPUT")[:80].replace("\n", " "))
    open(os.path.join(out, a.name + ".prompt.txt"), "w").write(build_prompt("<DDL>", notes, "<QUESTION>") + "\n")

if __name__ == "__main__":
    main()
