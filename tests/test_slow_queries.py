import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "query-plan-review"
STATS = SKILL / "evals" / "files" / "stats"
sys.path.insert(0, str(SKILL / "scripts"))

import slow_queries  # noqa: E402

HEADER = "queryid,calls,total_exec_time,mean_exec_time,rows,query\n"
LOOKUP = 'SELECT * FROM "items" WHERE "items"."order_id" = $1'


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = slow_queries.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class FixtureTests(unittest.TestCase):
    def report(self, name, *args):
        code, out, _ = run([STATS / name, "--json"] + list(args))
        return code, json.loads(out)

    def test_slow_fixture_flags_the_per_order_item_lookup(self):
        code, report = self.report("pg_stat_statements.csv")
        self.assertEqual(code, 1)
        self.assertEqual(report["statement_count"], 11)
        high = [c for c in report["n_plus_one_candidates"] if c["severity"] == "high"]
        self.assertEqual(len(high), 1)
        candidate = high[0]
        self.assertEqual(candidate["line"], 10)
        self.assertEqual(candidate["queryid"], "-7304118220194465512")
        self.assertEqual(candidate["lookup"], {"table": "shop_orderitem",
                                               "key_columns": ["shop_orderitem.order_id"]})
        self.assertEqual(candidate["calls"], 485760)
        self.assertEqual(candidate["rows_per_call"], 3.0)
        self.assertEqual(report["n_plus_one_candidates"][0], candidate)

    def test_slow_fixture_ranks_by_total_time(self):
        _, report = self.report("pg_stat_statements.csv")
        totals = [s["total_ms"] for s in report["top"]]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertIn("date_trunc", report["top"][0]["query"])
        self.assertEqual(report["top"][0]["line"], 2)
        self.assertIn('"external_ref" = $1', report["top"][1]["query"])
        self.assertEqual(report["top"][1]["line"], 8)  # the first query spans lines 2 to 7
        self.assertAlmostEqual(report["total_ms"], sum(s["total_ms"] for s in report["top"])
                               + 32.624, places=3)  # --top 10 leaves out BEGIN

    def test_slow_fixture_does_not_flag_writes_or_statements_without_a_lookup(self):
        _, report = self.report("pg_stat_statements.csv")
        flagged = " ".join(c["query"] for c in report["n_plus_one_candidates"])
        for text in ("UPDATE", "INSERT", "SELECT $1", "COMMIT", "LIKE UPPER"):
            self.assertNotIn(text, flagged)

    def test_clean_fixture_has_no_blocking_candidate(self):
        code, report = self.report("pg_stat_statements-clean.csv")
        self.assertEqual(code, 0)
        self.assertEqual(report["blocking_findings"], 0)
        self.assertEqual([c["severity"] for c in report["n_plus_one_candidates"]], ["medium"] * 3)
        self.assertNotIn("IN ($1", " ".join(c["query"] for c in report["n_plus_one_candidates"]))
        self.assertEqual(self.report("pg_stat_statements-clean.csv", "--fail-on", "medium")[0], 1)

    def test_text_output_names_line_and_numbers(self):
        code, text, _ = run([STATS / "pg_stat_statements.csv"])
        self.assertEqual(code, 1)
        self.assertIn("[high] line 10 (queryid -7304118220194465512): 485,760 calls x 0.412 ms", text)
        self.assertIn("table: shop_orderitem   lookup key: shop_orderitem.order_id", text)
        self.assertIn("RESULT: 1 high, 2 medium N+1 candidate(s).", text)

    def test_output_is_deterministic(self):
        first = run([STATS / "pg_stat_statements.csv", "--json"])
        self.assertEqual(first, run([STATS / "pg_stat_statements.csv", "--json"]))


class LookupShapeTests(unittest.TestCase):
    def test_parameter_equality_gives_table_and_key(self):
        self.assertEqual(slow_queries.lookup_shape(LOOKUP), ("items", ["items.order_id"]))
        self.assertEqual(slow_queries.lookup_shape("select id from public.users where email = ? limit 1"),
                         ("public.users", ["email"]))

    def test_leading_comment_is_skipped(self):
        query = "/* controller=orders */ -- tag\n SELECT id FROM orders WHERE customer_id = $1"
        self.assertEqual(slow_queries.lookup_shape(query), ("orders", ["customer_id"]))

    def test_other_shapes_are_not_lookups(self):
        for query in (
            "SELECT $1",
            "SELECT id FROM orders",
            "SELECT id FROM orders WHERE created_at >= $1",
            "SELECT id FROM orders WHERE id IN ($1, $2, $3)",
            "SELECT o.id FROM orders o JOIN items i ON i.order_id = o.id WHERE o.total > $1",
            "UPDATE orders SET status = $1 WHERE id = $2",
            "DELETE FROM orders WHERE id = $1",
            "WITH x AS (SELECT 1) SELECT * FROM x WHERE a = $1",
        ):
            self.assertIsNone(slow_queries.lookup_shape(query), query)


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pgss.csv"

    def report(self, text, *args):
        self.path.write_text(text, encoding="utf-8")
        code, out, err = run([self.path, "--json"] + list(args))
        return code, (json.loads(out) if out else None), err

    def test_thresholds_decide_candidates(self):
        text = HEADER + '1,999,400.0,0.4,999,"%s"\n2,5000,40000.0,8.0,5000,"%s"\n' % (
            LOOKUP.replace('"', '""'), LOOKUP.replace('"', '""').replace("items", "parts"))
        code, report, _ = self.report(text)
        self.assertEqual((code, report["n_plus_one_candidates"]), (0, []))
        _, report, _ = self.report(text, "--min-calls", "500")
        self.assertEqual([c["lookup"]["table"] for c in report["n_plus_one_candidates"]], ["items"])
        _, report, _ = self.report(text, "--max-mean-ms", "10")
        self.assertEqual([c["lookup"]["table"] for c in report["n_plus_one_candidates"]], ["parts"])

    def test_share_of_total_time_sets_severity(self):
        text = HEADER + '1,10000,1000.0,0.1,10000,"%s"\n2,10,99000.0,9900.0,10,SELECT pg_sleep($1)\n' % (
            LOOKUP.replace('"', '""'))
        code, report, _ = self.report(text)
        self.assertEqual(code, 0)
        self.assertEqual(report["n_plus_one_candidates"][0]["severity"], "medium")
        code, report, _ = self.report(text, "--min-share", "0.005")
        self.assertEqual(code, 1)
        self.assertEqual(report["n_plus_one_candidates"][0]["severity"], "high")

    def test_old_column_names_and_missing_mean_are_accepted(self):
        text = 'query,calls,total_time\n"%s",20000,8000.0\n' % LOOKUP.replace('"', '""')
        code, report, _ = self.report(text)
        self.assertEqual(code, 1)
        self.assertEqual(report["time_columns"], ["total_time"])
        candidate = report["n_plus_one_candidates"][0]
        self.assertAlmostEqual(candidate["mean_ms"], 0.4)
        self.assertIsNone(candidate["rows_per_call"])
        self.assertIsNone(candidate["queryid"])

    def test_lookup_is_recognised_behind_a_line_comment(self):
        text = HEADER + '1,5000,500.0,0.1,5000,"-- orders#show\n%s"\n' % LOOKUP.replace('"', '""')
        _, report, _ = self.report(text)
        self.assertEqual(len(report["n_plus_one_candidates"]), 1)
        self.assertIsNone(self.report(HEADER + "1,5000,500.0,0.1,5000,SELECT 1\n")[1]["top"][0]["lookup"])

    def test_header_case_and_byte_order_mark_are_tolerated(self):
        text = "﻿Query,Calls,Total_Exec_Time\nSELECT 1,5,2.5\n"
        code, report, _ = self.report(text)
        self.assertEqual(code, 0)
        self.assertEqual(report["statement_count"], 1)

    def test_unreadable_rows_are_skipped_and_reported(self):
        text = HEADER + "1,10,5.0,0.5,10,SELECT 1\n2,many,5.0,0.5,10,SELECT 2\n3,10,5.0\n\n4,4,8.0,2.0,4,SELECT 4\n"
        code, report, _ = self.report(text)
        self.assertEqual(code, 0)
        self.assertEqual(report["statement_count"], 2)
        self.assertEqual(report["skipped_lines"], [3, 4])
        self.assertEqual([s["line"] for s in report["top"]], [6, 2])

    def test_ties_keep_file_order(self):
        text = HEADER + "1,1,5.0,5.0,1,SELECT b\n2,1,5.0,5.0,1,SELECT a\n"
        _, report, _ = self.report(text)
        self.assertEqual([s["query"] for s in report["top"]], ["SELECT b", "SELECT a"])

    def test_bad_inputs_exit_two_with_a_message(self):
        cases = {
            "empty": ("", "the file is empty"),
            "no header": ("SELECT 1,5,2.5\n", "missing column(s)"),
            "missing total": ("query,calls\nSELECT 1,5\n", "total_exec_time (or total_time)"),
            "no data": (HEADER, "no usable data rows"),
            "only bad rows": (HEADER + "1,x,y,z,1,SELECT 1\n", "no usable data rows"),
        }
        for label, (text, message) in cases.items():
            code, report, err = self.report(text)
            self.assertEqual(code, 2, label)
            self.assertIsNone(report, label)
            self.assertIn(message, err, label)
            self.assertNotIn("Traceback", err, label)

    def test_missing_file_exits_two(self):
        code, _, err = run([Path(self.tmp.name) / "nope.csv"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_bad_option_value_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
            slow_queries.main([str(STATS / "pg_stat_statements.csv"), "--top", "-1"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
