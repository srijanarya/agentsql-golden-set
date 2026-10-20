import json, os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import eval as ev

class TestEquivalence(unittest.TestCase):
    def test_order_insensitive_and_column_names_ignored(self):
        self.assertTrue(ev.results_equivalent([("a", 1), ("b", 2)], [("b", 2), ("a", 1)])[0])
    def test_ordered_mode_requires_order(self):
        self.assertFalse(ev.results_equivalent([("a", 1), ("b", 2)], [("b", 2), ("a", 1)], ordered=True)[0])
    def test_numeric_tolerance_and_int_float(self):
        self.assertTrue(ev.results_equivalent([(10,)], [(10.004,)])[0])
        self.assertFalse(ev.results_equivalent([(10,)], [(10.5,)])[0])
        self.assertTrue(ev.results_equivalent([(0.1902,)], [(0.19021234,)], abs_tol=1e-4)[0])
    def test_null_and_shape_mismatch(self):
        self.assertTrue(ev.results_equivalent([(None,)], [(None,)])[0])
        self.assertFalse(ev.results_equivalent([(None,)], [(0,)])[0])
        self.assertFalse(ev.results_equivalent([(1, 2)], [(1,)])[0])
        self.assertFalse(ev.results_equivalent([(1,), (1,)], [(1,)])[0])
    def test_column_names_do_not_matter_end_to_end(self):
        db = self._db()
        a, _ = ev.run_sql(db, "SELECT COUNT(*) AS n FROM t"); b, _ = ev.run_sql(db, "SELECT COUNT(*) AS totally_different FROM t")
        self.assertTrue(ev.results_equivalent(a, b)[0])
    def _db(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "t.db"); c = sqlite3.connect(p)
        c.execute("CREATE TABLE t (x INTEGER)"); c.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)]); c.commit(); c.close(); return p

class TestGuard(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(); self.db = os.path.join(d, "t.db"); c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE t (x INTEGER)"); c.execute("INSERT INTO t VALUES (1)"); c.commit(); c.close()
    def count(self):
        c = sqlite3.connect(self.db); n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]; c.close(); return n
    def test_rejects_writes_and_multi_statement(self):
        for bad in ["INSERT INTO t VALUES (2)", "UPDATE t SET x = 9", "DELETE FROM t", "DROP TABLE t",
                    "SELECT 1; DROP TABLE t", "SELECT 1; SELECT 2", "WITH a AS (SELECT 1) DELETE FROM t",
                    "PRAGMA writable_schema=1", "/* hi */ INSERT INTO t VALUES (3)", "ATTACH DATABASE 'x' AS y"]:
            rows, err = ev.run_sql(self.db, bad)
            self.assertTrue(err and err.startswith("REJECTED"), bad)
        self.assertEqual(self.count(), 1)
    def test_allows_selects_incl_semicolons_in_strings_and_trailing(self):
        for ok in ["SELECT x FROM t;", "WITH a AS (SELECT 1 AS v) SELECT v FROM a", "SELECT ';' AS s -- trailing comment",
                   "SELECT replace('abc', 'b', 'z')", "SELECT 'DROP TABLE t' AS s"]:
            rows, err = ev.run_sql(self.db, ok); self.assertIsNone(err, (ok, err))
    def test_read_only_connection_blocks_writes_even_if_guard_bypassed(self):
        con = ev.connect_ro(self.db)
        with self.assertRaises(sqlite3.DatabaseError): con.execute("INSERT INTO t VALUES (5)")
        con.close(); self.assertEqual(self.count(), 1)

class TestRegressionDiff(unittest.TestCase):
    def rep(self, **st):
        return {"summary": {"accuracy": sum(v == "pass" for v in st.values()) / len(st)},
                "results": [{"id": k, "status": v} for k, v in st.items()]}
    def test_flags_passed_before_fails_now(self):
        d = ev.diff_reports(self.rep(Q1="pass", Q2="pass", Q3="fail", Q4="fail"), self.rep(Q1="pass", Q2="fail", Q3="pass", Q4="error"))
        self.assertEqual(d["regressions"], ["Q2"]); self.assertEqual(d["fixes"], ["Q3"])
    def test_cli_exit_code_and_report(self):
        d = tempfile.mkdtemp(); gold = os.path.join(d, "g.yaml"); db = os.path.join(d, "t.db")
        c = sqlite3.connect(db); c.execute("CREATE TABLE t (x)"); c.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)]); c.commit(); c.close()
        with open(gold, "w") as f: json.dump({"questions": [{"id": "Q1", "difficulty": "easy", "expected_result": {"rows": [[2]]}}]}, f)
        def run(name, sql, base=None):
            p = os.path.join(d, name + ".jsonl")
            with open(p, "w") as f: f.write(json.dumps({"id": "Q1", "sql": sql}) + "\n")
            args = [p, "--db", db, "--golden", gold, "--out", os.path.join(d, name + ".json")] + (["--baseline", base] if base else [])
            return ev.main(args)
        self.assertEqual(run("good", "SELECT COUNT(*) FROM t"), 0)
        self.assertEqual(run("bad", "SELECT COUNT(*) FROM t WHERE x > 1", os.path.join(d, "good.json")), 1)
        with open(os.path.join(d, "bad.json")) as f: self.assertEqual(json.load(f)["diff_vs_baseline"]["regressions"], ["Q1"])

if __name__ == "__main__":
    unittest.main()
