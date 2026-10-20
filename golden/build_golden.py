#!/usr/bin/env python3
"""Build golden/questions.yaml. Every question carries:
  sql        - the reference ("expected") SQL
  check_sql  - an independently written second query (different join/aggregation shape)
  py         - (where cheap) a pure-Python recomputation from raw table rows
The build FAILS unless sql, check_sql and py all agree on the result. Both queries and the
verified result are recorded in the output. (questions.yaml is JSON, which is valid YAML 1.2,
so it needs no third-party parser.)  Pinned as-of date for relative dates: 2026-10-15.
"""
import json, os, sqlite3, sys
from datetime import date, datetime, timedelta
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from eval import run_sql, results_equivalent

DB = os.path.join(ROOT, "data", "sample.db")
ASOF = "Assume today is 2026-10-15. "
VALID = "o.status = 'completed' AND o.is_deleted = 0"

# ---- raw tables for the Python recomputations
_c = sqlite3.connect(DB); _c.row_factory = sqlite3.Row
T = {t: [dict(r) for r in _c.execute(f"SELECT * FROM {t}")] for t in
     ("regions", "customers", "products", "orders", "order_items", "invoices", "payments")}
_c.close()
cust = {c["customer_id"]: c for c in T["customers"]}
orders = {o["order_id"]: o for o in T["orders"]}
inv = {i["invoice_id"]: i for i in T["invoices"]}
valid = lambda o: o["status"] == "completed" and o["is_deleted"] == 0
def line(i): return i["quantity"] * i["unit_price"] * (1 - (i["discount_pct"] or 0) / 100)
def ordval(oid): return sum(line(i) for i in T["order_items"] if i["order_id"] == oid)
def ist(ts): return (datetime.fromisoformat(ts) + timedelta(minutes=330)).date().isoformat()
def r2(x): return round(x, 2)
def net_collected(lo, hi): return sum(p["amount"] for p in T["payments"] if lo <= ist(p["paid_at_utc"]) < hi)
def inv_ok(i): return i["status"] != "void" and valid(orders[i["order_id"]])
def last_order_dates():
    d = {}
    for o in T["orders"]:
        if valid(o) and cust[o["customer_id"]]["is_deleted"] == 0:
            d[o["customer_id"]] = max(d.get(o["customer_id"], ""), o["order_date"])
    return d
def completed_counts():
    d = {}
    for o in T["orders"]:
        if valid(o) and cust[o["customer_id"]]["is_deleted"] == 0: d[o["customer_id"]] = d.get(o["customer_id"], 0) + 1
    return d
def units_q3():
    u = {}
    for i in T["order_items"]:
        o = orders[i["order_id"]]; p = next(p for p in T["products"] if p["product_id"] == i["product_id"])
        if valid(o) and "2026-07-01" <= o["order_date"] <= "2026-09-30" and p["is_deleted"] == 0:
            u[p["sku"]] = u.get(p["sku"], 0) + i["quantity"]
    return u
def top_with_ties(u, n):
    s = sorted(u.items(), key=lambda kv: (-kv[1], kv[0])); cut = s[n - 1][1]
    return [(k, v) for k, v in s if v >= cut]
def region_ratio():
    out = []
    for r in T["regions"]:
        pos = neg = 0.0; n = 0
        for p in T["payments"]:
            o = orders[inv[p["invoice_id"]]["order_id"]]; c = cust[o["customer_id"]]
            if c["is_deleted"] == 0 and valid(o) and c["region_id"] == r["region_id"]:
                n += 1; pos += p["amount"] if p["amount"] > 0 else 0; neg += -p["amount"] if p["amount"] < 0 else 0
        out.append((r["name"], None if pos == 0 else round(neg / pos, 4)))
    return out

Q = []
def q(id, diff, text, sql, check_sql, py=None, ordered=False, abs_tol=0.01, trap=""):
    Q.append(dict(id=id, difficulty=diff, question=text, sql=sql, check_sql=check_sql, py=py, ordered=ordered, abs_tol=abs_tol, trap=trap))

# ------------------------------------------------------------------ easy
q("Q01", "easy", "How many active (not soft-deleted) customers do we have?",
  "SELECT COUNT(*) FROM customers WHERE is_deleted = 0",
  "SELECT COUNT(*) FROM (SELECT customer_id FROM customers EXCEPT SELECT customer_id FROM customers WHERE is_deleted <> 0)",
  lambda: [(sum(c["is_deleted"] == 0 for c in T["customers"]),)], trap="soft-deleted rows")
q("Q02", "easy", "How many completed orders are there, excluding soft-deleted orders?",
  "SELECT COUNT(*) FROM orders o WHERE " + VALID,
  "SELECT SUM(CASE WHEN status = 'completed' AND is_deleted = 0 THEN 1 ELSE 0 END) FROM orders",
  lambda: [(sum(valid(o) for o in T["orders"]),)], trap="status + soft-delete")
q("Q03", "easy", "How many active (not soft-deleted) products are there in each category?",
  "SELECT category, COUNT(*) AS n FROM products WHERE is_deleted = 0 GROUP BY category",
  "SELECT category, SUM(1 - is_deleted) FROM products GROUP BY category HAVING SUM(1 - is_deleted) > 0",
  lambda: [(c, sum(1 for p in T["products"] if p["category"] == c and p["is_deleted"] == 0))
           for c in sorted({p["category"] for p in T["products"] if p["is_deleted"] == 0})], trap="soft-deleted product")
q("Q04", "easy", "Among active customers, how many distinct customers (count customers, not names) have placed at least one completed, non-deleted order?",
  "SELECT COUNT(DISTINCT o.customer_id) FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE c.is_deleted = 0 AND " + VALID,
  "SELECT COUNT(*) FROM customers c WHERE c.is_deleted = 0 AND EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id AND o.status = 'completed' AND o.is_deleted = 0)",
  lambda: [(len(completed_counts()),)], trap="duplicate customer names: count ids, not names")
q("Q05", "easy", "How many active customers have no region assigned?",
  "SELECT COUNT(*) FROM customers WHERE region_id IS NULL AND is_deleted = 0",
  "SELECT COUNT(*) FROM customers c LEFT JOIN regions r ON r.region_id = c.region_id WHERE r.region_id IS NULL AND c.is_deleted = 0",
  lambda: [(sum(c["region_id"] is None and c["is_deleted"] == 0 for c in T["customers"]),)], trap="NULL foreign key")
q("Q06", "easy", "List the SKUs of active products in the Hardware category.",
  "SELECT sku FROM products WHERE category = 'Hardware' AND is_deleted = 0",
  "SELECT p.sku FROM products p WHERE p.is_deleted = 0 AND p.category IN ('Hardware')",
  lambda: [(p["sku"],) for p in T["products"] if p["category"] == "Hardware" and p["is_deleted"] == 0], trap="soft-deleted product")
# ------------------------------------------------------------------ medium
q("Q07", "medium", ASOF + "What was the gross invoiced amount last quarter? Count invoices dated in that calendar quarter, excluding void invoices and invoices belonging to cancelled or soft-deleted orders.",
  "SELECT ROUND(SUM(i.amount), 2) FROM invoices i JOIN orders o ON o.order_id = i.order_id WHERE i.status <> 'void' AND " + VALID + " AND i.invoice_date >= '2026-07-01' AND i.invoice_date < '2026-10-01'",
  "SELECT ROUND(SUM(amount), 2) FROM invoices WHERE status != 'void' AND invoice_date BETWEEN '2026-07-01' AND '2026-09-30' AND order_id IN (SELECT order_id FROM orders WHERE status = 'completed' AND is_deleted = 0)",
  lambda: [(r2(sum(i["amount"] for i in T["invoices"] if inv_ok(i) and "2026-07-01" <= i["invoice_date"] <= "2026-09-30")),)],
  trap="relative date ('last quarter') pinned to as-of; void/cancelled/deleted exclusions; quarter boundary")
q("Q08", "medium", "What was the net cash collected in September 2026 (payments minus refunds), using India time (IST, UTC+05:30) for the payment date? Payment timestamps are stored in UTC. Sum all payment rows, with no filtering on orders or customers.",
  "SELECT ROUND(SUM(amount), 2) FROM payments WHERE datetime(paid_at_utc, '+330 minutes') >= '2026-09-01 00:00:00' AND datetime(paid_at_utc, '+330 minutes') < '2026-10-01 00:00:00'",
  "SELECT ROUND(SUM(amount), 2) FROM payments WHERE strftime('%Y-%m', datetime(paid_at_utc, '+5 hours', '+30 minutes')) = '2026-09'",
  lambda: [(r2(net_collected("2026-09-01", "2026-10-01")),)], trap="timezone edge; refunds are negative rows")
q("Q09", "medium", "What is the total amount refunded to customers across all time, as a positive number? (Refunds are negative payment rows.)",
  "SELECT ROUND(-SUM(amount), 2) FROM payments WHERE amount < 0",
  "SELECT ROUND(ABS(SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END)), 2) FROM payments",
  lambda: [(r2(-sum(p["amount"] for p in T["payments"] if p["amount"] < 0)),)], trap="negative amounts")
q("Q10", "medium", "Which active customers are tied for the most completed (non-deleted) orders? Return customer_id and the order count for every customer tied for first.",
  "WITH n AS (SELECT o.customer_id, COUNT(*) AS cnt FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE c.is_deleted = 0 AND " + VALID + " GROUP BY o.customer_id) SELECT customer_id, cnt FROM n WHERE cnt = (SELECT MAX(cnt) FROM n)",
  "SELECT customer_id, cnt FROM (SELECT o.customer_id, COUNT(*) AS cnt, RANK() OVER (ORDER BY COUNT(*) DESC) AS rk FROM orders o WHERE o.status = 'completed' AND o.is_deleted = 0 AND o.customer_id IN (SELECT customer_id FROM customers WHERE is_deleted = 0) GROUP BY o.customer_id) WHERE rk = 1",
  lambda: (lambda d: [(k, v) for k, v in d.items() if v == max(d.values())])(completed_counts()), trap="top-N with ties (LIMIT 1 is wrong)")
q("Q11", "medium", ASOF + "What is the average order value over completed, non-deleted orders dated in the 90 days ending 2026-10-15 inclusive? Order value = sum of quantity * unit_price * (1 - discount_pct/100) over its lines; a NULL discount means no discount. Round to 2 decimals.",
  "WITH v AS (SELECT o.order_id, SUM(i.quantity * i.unit_price * (1 - COALESCE(i.discount_pct, 0) / 100.0)) AS val FROM orders o JOIN order_items i ON i.order_id = o.order_id WHERE " + VALID + " AND o.order_date BETWEEN date('2026-10-15', '-89 days') AND '2026-10-15' GROUP BY o.order_id) SELECT ROUND(AVG(val), 2) FROM v",
  "SELECT ROUND(SUM(i.quantity * i.unit_price * (100 - IFNULL(i.discount_pct, 0)) / 100.0) / COUNT(DISTINCT o.order_id), 2) FROM orders o JOIN order_items i USING (order_id) WHERE o.status = 'completed' AND o.is_deleted = 0 AND o.order_date >= '2026-07-18' AND o.order_date <= '2026-10-15'",
  lambda: (lambda vs: [(r2(sum(vs) / len(vs)),)])([ordval(o["order_id"]) for o in T["orders"] if valid(o) and "2026-07-18" <= o["order_date"] <= "2026-10-15"]),
  trap="NULL discount; 90-day window arithmetic; per-order not per-line average")
q("Q12", "medium", "What was the total invoiced amount per month for June, July and August 2026 (by invoice date; exclude void invoices and invoices on cancelled or soft-deleted orders)? Return month as YYYY-MM and total, ordered by month.",
  "SELECT strftime('%Y-%m', i.invoice_date) AS month, ROUND(SUM(i.amount), 2) AS total FROM invoices i JOIN orders o ON o.order_id = i.order_id WHERE i.status <> 'void' AND " + VALID + " AND i.invoice_date >= '2026-06-01' AND i.invoice_date < '2026-09-01' GROUP BY month ORDER BY month",
  "SELECT substr(invoice_date, 1, 7), ROUND(SUM(amount), 2) FROM invoices WHERE order_id IN (SELECT order_id FROM orders WHERE status = 'completed' AND is_deleted = 0) AND status IN ('issued', 'paid') AND substr(invoice_date, 1, 7) IN ('2026-06', '2026-07', '2026-08') GROUP BY 1 ORDER BY 1",
  lambda: [(m, r2(sum(i["amount"] for i in T["invoices"] if inv_ok(i) and i["invoice_date"][:7] == m))) for m in ("2026-06", "2026-07", "2026-08")],
  ordered=True, trap="month boundaries; ordered output")
q("Q13", "medium", "For Q2 2026 (April-June, by invoice date), what share of non-void invoices on completed, non-deleted orders have status 'paid'? Return a ratio between 0 and 1 rounded to 4 decimals.",
  "SELECT ROUND(1.0 * SUM(CASE WHEN i.status = 'paid' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 4) FROM invoices i JOIN orders o ON o.order_id = i.order_id WHERE i.status <> 'void' AND " + VALID + " AND i.invoice_date >= '2026-04-01' AND i.invoice_date < '2026-07-01'",
  "SELECT ROUND(CAST((SELECT COUNT(*) FROM invoices i WHERE i.status = 'paid' AND i.invoice_date BETWEEN '2026-04-01' AND '2026-06-30' AND i.order_id IN (SELECT order_id FROM orders WHERE status = 'completed' AND is_deleted = 0)) AS REAL) / (SELECT COUNT(*) FROM invoices i WHERE i.status IN ('issued', 'paid') AND i.invoice_date BETWEEN '2026-04-01' AND '2026-06-30' AND i.order_id IN (SELECT order_id FROM orders WHERE status = 'completed' AND is_deleted = 0)), 4)",
  lambda: (lambda xs: [(round(sum(i["status"] == "paid" for i in xs) / len(xs), 4),)])([i for i in T["invoices"] if inv_ok(i) and "2026-04-01" <= i["invoice_date"] <= "2026-06-30"]),
  abs_tol=1e-4, trap="ratio; integer-division pitfall; void exclusion")
q("Q14", "medium", "How many non-deleted orders (any status) are dated on each of 2026-06-30 and 2026-07-01? Return the date and the count.",
  "SELECT order_date, COUNT(*) AS n FROM orders WHERE is_deleted = 0 AND order_date IN ('2026-06-30', '2026-07-01') GROUP BY order_date",
  "SELECT d.order_date, (SELECT COUNT(*) FROM orders o WHERE o.order_date = d.order_date AND o.is_deleted = 0) FROM (SELECT '2026-06-30' AS order_date UNION ALL SELECT '2026-07-01') d",
  lambda: [(d, sum(o["order_date"] == d and o["is_deleted"] == 0 for o in T["orders"])) for d in ("2026-06-30", "2026-07-01")], trap="quarter boundary dates")
q("Q15", "medium", "Which customer names are shared by more than one active customer? Return the name and the number of active customers with that name.",
  "SELECT name, COUNT(*) AS n FROM customers WHERE is_deleted = 0 GROUP BY name HAVING COUNT(*) > 1",
  "SELECT a.name, COUNT(*) FROM customers a WHERE a.is_deleted = 0 AND EXISTS (SELECT 1 FROM customers b WHERE b.name = a.name AND b.is_deleted = 0 AND b.customer_id <> a.customer_id) GROUP BY a.name",
  lambda: (lambda d: [(k, v) for k, v in d.items() if v > 1])(
      {n: sum(1 for c in T["customers"] if c["name"] == n and c["is_deleted"] == 0) for n in {c["name"] for c in T["customers"]}}),
  trap="soft-deleted duplicate must not count (Iyer Retail)")
# ------------------------------------------------------------------ hard
q("Q16", "hard", "For each region, what is the refund ratio: total refunds (as a positive number) divided by total positive payments received, considering active customers and completed non-deleted orders only? Return region name and the ratio rounded to 4 decimals; list every region, and return NULL where the region has no payments.",
  "SELECT r.name, ROUND(COALESCE(-SUM(CASE WHEN p.amount < 0 THEN p.amount END), 0) / NULLIF(SUM(CASE WHEN p.amount > 0 THEN p.amount END), 0), 4) AS refund_ratio FROM regions r LEFT JOIN customers c ON c.region_id = r.region_id AND c.is_deleted = 0 LEFT JOIN orders o ON o.customer_id = c.customer_id AND " + VALID + " LEFT JOIN invoices i ON i.order_id = o.order_id LEFT JOIN payments p ON p.invoice_id = i.invoice_id GROUP BY r.region_id, r.name",
  "WITH pay AS (SELECT c.region_id AS rid, SUM(CASE WHEN p.amount > 0 THEN p.amount ELSE 0 END) AS pos, SUM(CASE WHEN p.amount < 0 THEN -p.amount ELSE 0 END) AS neg FROM payments p JOIN invoices i ON i.invoice_id = p.invoice_id JOIN orders o ON o.order_id = i.order_id JOIN customers c ON c.customer_id = o.customer_id WHERE c.is_deleted = 0 AND o.status = 'completed' AND o.is_deleted = 0 GROUP BY c.region_id) SELECT r.name, CASE WHEN pay.pos IS NULL OR pay.pos = 0 THEN NULL ELSE ROUND(pay.neg / pay.pos, 4) END FROM regions r LEFT JOIN pay ON pay.rid = r.region_id",
  region_ratio, abs_tol=1e-4, trap="divide-by-zero (Jaipur never pays); LEFT JOINs keep empty regions; refunds as negatives")
q("Q17", "hard", "Top 3 products by units sold in Q3 2026 (orders dated July-September 2026; completed, non-deleted orders; active products only). If several products tie for 3rd place include all of them. Return sku and units, highest units first, ties broken by sku ascending.",
  "WITH u AS (SELECT p.sku, SUM(i.quantity) AS units FROM order_items i JOIN orders o ON o.order_id = i.order_id JOIN products p ON p.product_id = i.product_id WHERE " + VALID + " AND p.is_deleted = 0 AND o.order_date >= '2026-07-01' AND o.order_date < '2026-10-01' GROUP BY p.sku) SELECT sku, units FROM u WHERE units >= (SELECT units FROM u ORDER BY units DESC LIMIT 1 OFFSET 2) ORDER BY units DESC, sku",
  "SELECT sku, units FROM (SELECT p.sku AS sku, SUM(i.quantity) AS units, DENSE_RANK() OVER (ORDER BY SUM(i.quantity) DESC) AS dr, RANK() OVER (ORDER BY SUM(i.quantity) DESC) AS rk FROM products p JOIN order_items i ON i.product_id = p.product_id JOIN orders o ON o.order_id = i.order_id WHERE p.is_deleted = 0 AND o.status = 'completed' AND o.is_deleted = 0 AND o.order_date BETWEEN '2026-07-01' AND '2026-09-30' GROUP BY p.sku) WHERE rk <= 3 ORDER BY units DESC, sku",
  lambda: top_with_ties(units_q3(), 3), ordered=True, trap="top-N with ties at the cut-off; soft-deleted product; quarter boundary")
q("Q18", "hard", ASOF + "Which active customers have not placed a completed, non-deleted order in more than 120 days (their most recent such order is dated before 2026-06-17)? Return customer_id and last_order_date, ordered by last_order_date then customer_id. Customers with no orders are excluded.",
  "SELECT o.customer_id, MAX(o.order_date) AS last_order_date FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE c.is_deleted = 0 AND " + VALID + " GROUP BY o.customer_id HAVING MAX(o.order_date) < date('2026-10-15', '-120 days') ORDER BY last_order_date, o.customer_id",
  "SELECT customer_id, last_d FROM (SELECT customer_id, (SELECT MAX(order_date) FROM orders x WHERE x.customer_id = c.customer_id AND x.status = 'completed' AND x.is_deleted = 0) AS last_d FROM customers c WHERE c.is_deleted = 0) WHERE last_d IS NOT NULL AND julianday('2026-10-15') - julianday(last_d) > 120 ORDER BY last_d, customer_id",
  lambda: sorted([(k, v) for k, v in last_order_dates().items() if v < "2026-06-17"], key=lambda t: (t[1], t[0])), ordered=True,
  trap="relative date pinned to as-of; MAX not latest-row; customers without orders")
q("Q19", "hard", "What was the month-over-month growth in net cash collected (payments minus refunds, IST dates, summing all payment rows with no filtering on orders or customers) from August 2026 to September 2026, i.e. (September - August) / August, rounded to 4 decimals?",
  "WITH m AS (SELECT strftime('%Y-%m', datetime(paid_at_utc, '+330 minutes')) AS ym, SUM(amount) AS net FROM payments GROUP BY ym) SELECT ROUND((SELECT net FROM m WHERE ym = '2026-09') * 1.0 / (SELECT net FROM m WHERE ym = '2026-08') - 1, 4)",
  "SELECT ROUND((SUM(CASE WHEN datetime(paid_at_utc, '+330 minutes') >= '2026-09-01' AND datetime(paid_at_utc, '+330 minutes') < '2026-10-01' THEN amount END) - SUM(CASE WHEN datetime(paid_at_utc, '+330 minutes') >= '2026-08-01' AND datetime(paid_at_utc, '+330 minutes') < '2026-09-01' THEN amount END)) / SUM(CASE WHEN datetime(paid_at_utc, '+330 minutes') >= '2026-08-01' AND datetime(paid_at_utc, '+330 minutes') < '2026-09-01' THEN amount END), 4) FROM payments",
  lambda: (lambda a, s: [(round((s - a) / a, 4),)])(net_collected("2026-08-01", "2026-09-01"), net_collected("2026-09-01", "2026-10-01")),
  abs_tol=1e-4, trap="timezone edge payments straddle both month boundaries in IST; refunds")
q("Q20", "hard", "For each active customer who has received at least one refund, what is their net amount collected (all payments minus refunds) on invoices of non-deleted orders? Return customer_id and net amount, highest net first, ties broken by customer_id ascending.",
  "SELECT c.customer_id, ROUND(SUM(p.amount), 2) AS net FROM customers c JOIN orders o ON o.customer_id = c.customer_id AND o.is_deleted = 0 JOIN invoices i ON i.order_id = o.order_id JOIN payments p ON p.invoice_id = i.invoice_id WHERE c.is_deleted = 0 GROUP BY c.customer_id HAVING SUM(CASE WHEN p.amount < 0 THEN 1 ELSE 0 END) > 0 ORDER BY net DESC, c.customer_id",
  "SELECT cid, ROUND(net, 2) FROM (SELECT o.customer_id AS cid, SUM(p.amount) AS net, MIN(p.amount) AS worst FROM payments p, invoices i, orders o WHERE p.invoice_id = i.invoice_id AND i.order_id = o.order_id AND o.is_deleted = 0 AND o.customer_id NOT IN (SELECT customer_id FROM customers WHERE is_deleted = 1) GROUP BY o.customer_id) WHERE worst < 0 ORDER BY net DESC, cid",
  lambda: (lambda d: sorted([(k, r2(v[0])) for k, v in d.items() if v[1]], key=lambda t: (-t[1], t[0])))(
      (lambda acc: acc)(__import__("functools").reduce(lambda acc, p: (acc.__setitem__(orders[inv[p["invoice_id"]]["order_id"]]["customer_id"],
          (acc.get(orders[inv[p["invoice_id"]]["order_id"]]["customer_id"], (0, False))[0] + p["amount"],
           acc.get(orders[inv[p["invoice_id"]]["order_id"]]["customer_id"], (0, False))[1] or p["amount"] < 0)) or acc),
          [p for p in T["payments"] if orders[inv[p["invoice_id"]]["order_id"]]["is_deleted"] == 0
           and cust[orders[inv[p["invoice_id"]]["order_id"]]["customer_id"]]["is_deleted"] == 0], {}))),
  ordered=True, trap="soft-deleted orders still have payment rows; refunds net against payments")

def main():
    out, bad = [], 0
    for x in Q:
        a, ea = run_sql(DB, x["sql"]); b, eb = run_sql(DB, x["check_sql"])
        assert ea is None and eb is None, (x["id"], ea, eb)
        ok_b, why_b = results_equivalent(a, b, x["ordered"], x["abs_tol"])
        py = x["py"]() if x["py"] else None
        ok_p, why_p = results_equivalent(a, py, x["ordered"], x["abs_tol"]) if py is not None else (True, "n/a")
        if not (ok_b and ok_p) or not a:
            bad += 1; print("DISAGREE/EMPTY", x["id"], why_b, why_p, "ref:", a[:5], "check:", b[:5], "py:", (py or [])[:5])
        rows = [list(r) for r in a]
        out.append({"id": x["id"], "difficulty": x["difficulty"], "question": x["question"], "trap": x["trap"],
                    "ordered": x["ordered"], "abs_tol": x["abs_tol"], "expected_sql": x["sql"],
                    "verification": {"check_sql": x["check_sql"], "check_sql_agrees": ok_b,
                                     "python_recomputation": "agrees" if (py is not None and ok_p) else "not-applicable",
                                     "python_result": [list(r) for r in py] if py is not None else None},
                    "expected_result": {"columns_note": "compared by position, not name", "rows": rows}})
        print(f"{x['id']} {x['difficulty']:<6} rows={len(rows):<3} sql==check:{ok_b} py:{'ok' if py is not None and ok_p else 'n/a'}  first={rows[:2]}")
    if bad: sys.exit(f"{bad} questions failed verification; questions.yaml NOT written")
    path = os.path.join(ROOT, "golden", "questions.yaml")
    with open(path, "w") as f:
        f.write("# Golden question set. JSON syntax (valid YAML 1.2) so it parses with the stdlib. As-of date for relative questions: 2026-10-15.\n")
        json.dump({"as_of": "2026-10-15", "database": "data/sample.db", "questions": out}, f, indent=1); f.write("\n")
    print(f"wrote {path} ({len(out)} questions, all verified)")

if __name__ == "__main__":
    main()
