#!/usr/bin/env python3
"""Deterministically generate data/sample.db: a small Indian B2B distribution business.

Run: python3 gen_db.py   (stdlib only; same seed -> byte-identical logical content)

Deliberate traps (each is exercised by at least one golden question):
  * NULLs: customers.region_id, order_items.discount_pct
  * refunds: payments.amount < 0
  * duplicate customer names with different customer_ids
  * soft deletes: customers/products/orders .is_deleted = 1
  * cancelled orders (status) that must not count as revenue
  * payments.paid_at_utc is UTC; business reporting is IST (UTC+05:30), so a
    payment at 2026-08-31 20:00 UTC belongs to 1 September in IST
  * month/quarter boundary dates, tied top-N counts, a region with no payments
"""
import os, random, sqlite3
from datetime import date, datetime, timedelta

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sample.db")
AS_OF = date(2026, 10, 15)

SCHEMA = """
CREATE TABLE regions (region_id INTEGER PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE customers (
  customer_id INTEGER PRIMARY KEY, name TEXT NOT NULL, region_id INTEGER REFERENCES regions(region_id),
  segment TEXT NOT NULL, created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE products (
  product_id INTEGER PRIMARY KEY, sku TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
  list_price REAL NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
  order_date TEXT NOT NULL, status TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE order_items (
  item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(order_id),
  product_id INTEGER NOT NULL REFERENCES products(product_id),
  quantity INTEGER NOT NULL, unit_price REAL NOT NULL, discount_pct REAL);
CREATE TABLE invoices (
  invoice_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(order_id),
  invoice_date TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL);
CREATE TABLE payments (
  payment_id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(invoice_id),
  paid_at_utc TEXT NOT NULL, amount REAL NOT NULL, method TEXT NOT NULL);
"""

REGIONS = [("Mumbai", "Maharashtra"), ("Pune", "Maharashtra"), ("Delhi NCR", "Delhi"),
           ("Bengaluru", "Karnataka"), ("Chennai", "Tamil Nadu"), ("Kolkata", "West Bengal"),
           ("Ahmedabad", "Gujarat"), ("Jaipur", "Rajasthan")]   # Jaipur: customers but no payments
PRODUCTS = [("SKU-100", "Barcode Scanner", "Hardware", 4500), ("SKU-101", "Thermal Printer", "Hardware", 12000),
            ("SKU-102", "POS Terminal", "Hardware", 25000), ("SKU-103", "Receipt Roll (50)", "Consumables", 900),
            ("SKU-104", "Label Sheet Pack", "Consumables", 650), ("SKU-105", "Inventory Software Seat", "Software", 3000),
            ("SKU-106", "Billing Software Seat", "Software", 3500), ("SKU-107", "Onsite Install", "Services", 8000),
            ("SKU-108", "Annual Support", "Services", 15000), ("SKU-109", "Cash Drawer", "Hardware", 6500),
            ("SKU-110", "Legacy Dot Matrix Printer", "Hardware", 7000), ("SKU-111", "Training Day", "Services", 5000)]
NAMES = ["Sharma Traders", "Gupta Distributors", "Iyer Retail", "Patel Wholesale", "Khan Electronics",
         "Mehta Stores", "Reddy Supplies", "Bose Enterprises", "Nair Mart", "Singh & Sons", "Verma Agencies",
         "Joshi Cash & Carry", "Das Trading Co", "Kulkarni Bazaar", "Rao Hardware", "Malhotra Group",
         "Chopra Imports", "Pillai Stores", "Banerjee Foods", "Shah Pharma Dist", "Agarwal Paper", "Menon Hypermart",
         "Saxena Traders", "Thakur Retail", "Desai Logistics", "Kapoor Wholesale", "Ghosh Mart", "Yadav Dist",
         "Sethi Electronics", "Naidu Stores", "Bhat Agro", "Lal Chemists", "Hegde Retail", "Mishra Bros", "Dutta & Co"]
SEGMENTS = ["SMB", "Mid-market", "Enterprise"]

def main():
    rnd = random.Random(20261015)
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB): os.remove(DB)
    con = sqlite3.connect(DB); con.executescript(SCHEMA)
    con.executemany("INSERT INTO regions VALUES (?,?,?)", [(i + 1, n, s) for i, (n, s) in enumerate(REGIONS)])
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)",
                    [(i + 1, s, n, c, p, 1 if s == "SKU-110" else 0) for i, (s, n, c, p) in enumerate(PRODUCTS)])
    # customers: 35 distinct names + deliberate duplicates (different ids, regions)
    custs = []
    for i, n in enumerate(NAMES):
        region = None if i in (3, 17) else (i % 8) + 1          # two NULL regions
        custs.append([n, region, SEGMENTS[i % 3], str(date(2025, 6, 1) + timedelta(days=i * 5)), 0])
    custs.append(["Sharma Traders", 2, "SMB", "2025-09-01", 0])        # dup name, Pune
    custs.append(["Gupta Distributors", 3, "Mid-market", "2025-09-10", 0])   # dup name
    custs.append(["Iyer Retail", 4, "SMB", "2025-09-20", 1])           # dup name, soft-deleted
    custs.append(["Patel Wholesale", 7, "SMB", "2025-10-02", 0])        # dup name
    for idx in (9, 21, 33): custs[idx][4] = 1                          # soft-deleted customers
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", [(i + 1, *c) for i, c in enumerate(custs)])
    ncust = len(custs)
    jaipur = 8
    deleted_cust = {i + 1 for i, c in enumerate(custs) if c[4] == 1}

    order_id = item_id = inv_id = pay_id = 0
    def add_order(cid, d, status="completed", is_del=0, lines=None):
        nonlocal order_id, item_id, inv_id, pay_id
        order_id += 1
        con.execute("INSERT INTO orders VALUES (?,?,?,?,?)", (order_id, cid, d.isoformat(), status, is_del))
        total = 0.0
        for _ in range(lines or rnd.choice([1, 1, 2, 2, 3])):
            pid = rnd.randint(1, 12); qty = rnd.choice([1, 1, 2, 3, 5, 10]) if pid not in (1, 2, 3, 10) else rnd.choice([1, 1, 2, 3])
            price = PRODUCTS[pid - 1][3]
            disc = rnd.choice([None, None, 0, 5, 10, 15])               # NULL discount trap
            item_id += 1
            con.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)", (item_id, order_id, pid, qty, price, disc))
            total += qty * price * (1 - (disc or 0) / 100)
        if status == "cancelled": return
        inv_id += 1
        r = rnd.random(); ist = "void" if r < 0.03 else ("paid" if r < 0.80 else "issued")
        inv_date = d + timedelta(days=rnd.choice([0, 0, 1]))
        con.execute("INSERT INTO invoices VALUES (?,?,?,?,?)", (inv_id, order_id, inv_date.isoformat(), round(total, 2), ist))
        cust_region = custs[cid - 1][1]
        if ist == "paid" and cust_region != jaipur:
            pay_dt = datetime.combine(inv_date + timedelta(days=rnd.randint(1, 25)), datetime.min.time())
            if pay_dt.date() > AS_OF: pay_dt = datetime.combine(AS_OF, datetime.min.time())
            pay_dt += timedelta(hours=rnd.choice([3, 6, 9, 12, 15, 18, 19, 20, 22]), minutes=rnd.randint(0, 59))
            pay_id += 1
            con.execute("INSERT INTO payments VALUES (?,?,?,?,?)", (pay_id, inv_id, pay_dt.strftime("%Y-%m-%d %H:%M:%S"), round(total, 2), rnd.choice(["UPI", "NEFT", "Cheque"])))
            if rnd.random() < 0.10:                                      # refund: negative payment
                pay_id += 1
                con.execute("INSERT INTO payments VALUES (?,?,?,?,?)", (pay_id, inv_id, (pay_dt + timedelta(days=rnd.randint(2, 15))).strftime("%Y-%m-%d %H:%M:%S") if (pay_dt + timedelta(days=15)).date() <= AS_OF else pay_dt.strftime("%Y-%m-%d %H:%M:%S"), -round(total * rnd.choice([0.25, 0.5, 1.0]), 2), "Refund"))
        elif ist == "paid":
            con.execute("UPDATE invoices SET status='issued' WHERE invoice_id=?", (inv_id,))  # Jaipur never pays

    start = date(2025, 10, 1); span = (AS_OF - start).days
    for _ in range(300):
        cid = rnd.randint(1, ncust)
        d = start + timedelta(days=rnd.randint(0, span))
        r = rnd.random()
        add_order(cid, d, "cancelled" if r < 0.06 else "completed", 1 if 0.06 <= r < 0.10 else 0)
    # boundary-date orders (month / quarter edges)
    for d in ("2026-03-31", "2026-04-01", "2026-06-30", "2026-07-01", "2026-08-31", "2026-09-01", "2026-09-30", "2026-10-01"):
        add_order(rnd.choice([c for c in range(1, ncust + 1) if c not in deleted_cust]), date.fromisoformat(d))
    # explicit timezone-edge payments: UTC evening = next day in IST
    for inv, ts in ((None, "2026-08-31 19:30:00"), (None, "2026-08-31 20:15:00"), (None, "2026-09-30 18:45:00"), (None, "2026-09-30 18:29:00")):
        add_order(2, date(2026, 8, 25) if ts.startswith("2026-08") else date(2026, 9, 20), lines=1)
        con.execute("UPDATE invoices SET status='paid' WHERE invoice_id=?", (inv_id,))
        amt = con.execute("SELECT amount FROM invoices WHERE invoice_id=?", (inv_id,)).fetchone()[0]
        con.execute("DELETE FROM payments WHERE invoice_id=?", (inv_id,)); pay_id += 1
        con.execute("INSERT INTO payments VALUES (?,?,?,?,?)", (pay_id, inv_id, ts, amt, "UPI"))
    # engineered tie: customers 7 and 8 end up with identical (maximum) completed-order counts
    def completed(c):
        return con.execute("SELECT COUNT(*) FROM orders WHERE customer_id=? AND status='completed' AND is_deleted=0", (c,)).fetchone()[0]
    top = max(completed(c) for c in range(1, ncust + 1) if c not in deleted_cust and c not in (7, 8))
    for c in (7, 8):
        while completed(c) < top + 1:
            add_order(c, start + timedelta(days=rnd.randint(0, span)))
    con.commit(); con.close()
    print(f"wrote {DB}")

if __name__ == "__main__":
    main()
