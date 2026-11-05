import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "query-plan-review"
PLANS = SKILL / "evals" / "files" / "plans"
sys.path.insert(0, str(SKILL / "scripts"))

import plan_review  # noqa: E402


def node(node_type, rows=1, loops=1, ms=1.0, plan_rows=None, rel=None, plans=None, **extra):
    """Build one ANALYZE plan node. Pass ms=None for a TIMING OFF plan."""
    raw = {"Node Type": node_type, "Plan Rows": rows if plan_rows is None else plan_rows,
           "Actual Rows": rows, "Actual Loops": loops}
    if ms is not None:
        raw["Actual Total Time"] = ms
    if rel:
        raw["Parent Relationship"] = rel
    if plans:
        raw["Plans"] = plans
    for key, value in extra.items():
        raw[key.replace("_", " ")] = value
    return raw


def scan(removed, rows=10, ms=90.0, node_type="Seq Scan", **extra):
    extra.setdefault("Filter", "(x = 1)")
    extra["Rows Removed by Filter"] = removed
    return node(node_type, rows=rows, ms=ms, Relation_Name="t", **extra)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plan_review.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "plan.json"

    def review(self, plan, *args, **doc_extra):
        doc = {"Plan": plan}
        doc.update(doc_extra)
        self.path.write_text(json.dumps([doc]), encoding="utf-8")
        code, out, _ = run([self.path, "--json"] + list(args))
        return code, json.loads(out)["plans"][0]

    def kinds(self, plan, *args):
        return [f["kind"] for f in self.review(plan, *args)[1]["findings"]]


class FixtureTests(unittest.TestCase):
    def review(self, name, *args):
        code, out, _ = run([PLANS / name, "--json"] + list(args))
        return code, json.loads(out)["plans"][0]

    def test_seq_scan_fixture_reports_the_scan_with_its_numbers(self):
        code, plan = self.review("seq-scan.json")
        self.assertEqual(code, 1)
        self.assertEqual([f["kind"] for f in plan["findings"]], ["seq-scan-filter"])
        finding = plan["findings"][0]
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["path"], "[0].Plan.Plans[0].Plans[0]")
        self.assertEqual(finding["node"], "Seq Scan on shop_order")
        self.assertEqual(finding["numbers"]["rows_removed_by_filter"], 1499997)
        self.assertEqual(finding["numbers"]["rows_kept"], 3)
        self.assertIn("external_ref", " ".join(finding["evidence"]))
        self.assertEqual(plan["hot_nodes"][0]["path"], finding["path"])

    def test_misestimate_fixture_reports_the_loop_and_where_the_estimate_went_wrong(self):
        code, plan = self.review("misestimate.json")
        self.assertEqual(code, 1)
        self.assertEqual([(f["kind"], f["severity"]) for f in plan["findings"]],
                         [("nested-loop-inner", "high"), ("row-misestimate", "medium")])
        loop, estimate = plan["findings"]
        self.assertEqual(loop["numbers"]["loops"], 38640)
        self.assertEqual(loop["numbers"]["outer_plan_rows"], 14)
        self.assertAlmostEqual(loop["numbers"]["time_ms"], 425.04, places=2)
        self.assertEqual(estimate["path"], "[0].Plan.Plans[0]")
        self.assertEqual(estimate["numbers"]["factor"], 2760.0)
        self.assertEqual(estimate["numbers"]["direction"], "under")

    def test_sort_spill_fixture_reports_only_the_spill(self):
        code, plan = self.review("sort-spill.json")
        self.assertEqual(code, 1)
        self.assertEqual([f["kind"] for f in plan["findings"]], ["disk-spill"])
        finding = plan["findings"][0]
        self.assertEqual(finding["numbers"]["disk_kb"], 39664)
        self.assertEqual(finding["path"], "[0].Plan")
        self.assertIn("external merge", finding["message"])
        # exclusive time of the Sort: 1322.914 - 212.447
        self.assertAlmostEqual(finding["numbers"]["time_ms"], 1110.467, places=3)

    def test_index_filter_fixture_reports_the_index_scan(self):
        code, plan = self.review("index-filter.json")
        self.assertEqual(code, 1)
        self.assertEqual([f["kind"] for f in plan["findings"]], ["index-filter"])
        finding = plan["findings"][0]
        self.assertEqual(finding["node"], "Index Scan using tickets_status_idx on tickets")
        self.assertEqual(finding["numbers"]["rows_removed_by_filter"], 412870)
        self.assertIn("assignee_id", " ".join(finding["evidence"]))

    def test_estimate_only_fixture_says_so_and_does_not_block_by_default(self):
        code, plan = self.review("estimate-only.json")
        self.assertEqual(code, 0)
        self.assertFalse(plan["analyzed"])
        self.assertEqual([f["kind"] for f in plan["findings"]], ["no-analyze"])
        self.assertEqual(plan["hot_nodes"], [])
        self.assertIn("UPDATE on orders", plan["notes"][0])
        self.assertIn("would execute", plan["notes"][0])
        self.assertEqual(self.review("estimate-only.json", "--fail-on", "medium")[0], 1)
        _, text, _ = run([PLANS / "estimate-only.json"])
        self.assertIn("RESULT: NOT MEASURED", text)

    def test_healthy_fixture_has_no_findings_at_any_level(self):
        code, plan = self.review("healthy.json", "--fail-on", "medium")
        self.assertEqual(code, 0)
        self.assertEqual(plan["findings"], [])
        self.assertTrue(plan["analyzed"])
        _, text, _ = run([PLANS / "healthy.json"])
        self.assertIn("RESULT: no findings", text)

    def test_text_output_names_path_and_numbers(self):
        code, text, _ = run([PLANS / "seq-scan.json"])
        self.assertEqual(code, 1)
        self.assertIn("[high] seq-scan-filter  Seq Scan on shop_order", text)
        self.assertIn("path: [0].Plan.Plans[0].Plans[0]  (Limit > Sort > Seq Scan)", text)
        self.assertIn("read 1,500,000 rows; the filter discarded 1,499,997 and kept 3", text)
        self.assertIn("RESULT: 1 high, 0 medium finding(s).", text)

    def test_output_is_deterministic(self):
        first = run([PLANS / "misestimate.json", "--json"])
        self.assertEqual(first, run([PLANS / "misestimate.json", "--json"]))


class FilterTests(PlanCase):
    def test_scan_keeping_most_rows_is_not_reported(self):
        self.assertEqual(self.kinds(scan(removed=50000, rows=50000)), [])

    def test_small_table_is_not_reported(self):
        self.assertEqual(self.kinds(scan(removed=900, rows=1)), [])

    def test_per_loop_counts_are_multiplied_by_loops(self):
        plan = node("Nested Loop", rows=100, ms=100.0, plans=[
            node("Index Scan", rows=100, ms=1.0, rel="Outer"),
            scan(removed=500, rows=1, ms=0.9, loops=100, Parent_Relationship="Inner"),
        ])
        _, result = self.review(plan)
        finding = result["findings"][0]
        self.assertEqual(finding["kind"], "seq-scan-filter")
        self.assertEqual(finding["numbers"]["rows_removed_by_filter"], 50000)
        self.assertAlmostEqual(finding["numbers"]["time_ms"], 90.0)

    def test_scan_with_small_share_of_time_is_medium_and_does_not_block(self):
        plan = node("Hash Join", rows=10, ms=1000.0, plans=[
            node("Index Scan", rows=10, ms=950.0, rel="Outer"),
            scan(removed=5000, rows=10, ms=20.0, Parent_Relationship="Inner"),
        ])
        code, result = self.review(plan)
        self.assertEqual(code, 0)
        self.assertEqual([(f["kind"], f["severity"]) for f in result["findings"]],
                         [("seq-scan-filter", "medium")])
        self.assertEqual(self.review(plan, "--fail-on", "medium")[0], 1)
        self.assertEqual(self.review(plan, "--min-share", "0.01")[1]["findings"][0]["severity"], "high")

    def test_bitmap_heap_and_index_only_scans_are_index_filter(self):
        for node_type in ("Bitmap Heap Scan", "Index Only Scan"):
            self.assertEqual(self.kinds(scan(removed=20000, node_type=node_type)), ["index-filter"])

    def test_never_executed_node_is_ignored(self):
        plan = node("Append", rows=5, ms=5.0, plans=[
            scan(removed=99999, rows=0, loops=0, ms=0.0, Parent_Relationship="Member")])
        self.assertEqual(self.kinds(plan), [])

    def test_timing_off_keeps_finding_high_and_says_why(self):
        code, result = self.review(scan(removed=50000, ms=None))
        self.assertEqual(code, 1)
        self.assertEqual(result["findings"][0]["severity"], "high")
        self.assertIsNone(result["findings"][0]["numbers"]["time_ms"])
        self.assertEqual(result["hot_nodes"], [])
        self.assertIn("TIMING OFF", " ".join(result["notes"]))


class LoopTests(PlanCase):
    def nested(self, loops, inner_ms=0.01, outer_plan=None):
        return node("Nested Loop", rows=loops, ms=loops * inner_ms + 2.0, plans=[
            node("Index Scan", rows=loops, plan_rows=outer_plan, ms=1.0, rel="Outer"),
            node("Index Scan", rows=1, loops=loops, ms=inner_ms, rel="Inner", Index_Name="t_pkey"),
        ])

    def test_inner_side_over_threshold_is_reported_on_the_join(self):
        _, result = self.review(self.nested(5000))
        finding = result["findings"][0]
        self.assertEqual((finding["kind"], finding["path"]), ("nested-loop-inner", "[0].Plan"))
        self.assertEqual(finding["numbers"]["loops"], 5000)
        self.assertIn("t_pkey", finding["message"])

    def test_inner_side_under_threshold_is_not_reported(self):
        self.assertEqual(self.kinds(self.nested(999)), [])
        self.assertEqual(self.kinds(self.nested(999), "--min-loops", "500"), ["nested-loop-inner"])

    def test_correlated_subplan_is_reported(self):
        plan = node("Seq Scan", rows=4000, ms=400.0, plans=[
            node("Aggregate", rows=1, loops=4000, ms=0.09, rel="SubPlan", Subplan_Name="SubPlan 1")])
        _, result = self.review(plan)
        self.assertEqual(result["findings"][0]["kind"], "subplan-loop")
        self.assertAlmostEqual(result["findings"][0]["numbers"]["time_ms"], 360.0)


class SpillTests(PlanCase):
    def test_in_memory_sort_is_not_reported(self):
        plan = node("Sort", ms=50.0, Sort_Method="quicksort", Sort_Space_Used=512,
                    Sort_Space_Type="Memory")
        self.assertEqual(self.kinds(plan), [])

    def test_sort_spill_in_parallel_workers_is_summed(self):
        workers = [{"Worker Number": i, "Sort Method": "external merge", "Sort Space Used": 1000,
                    "Sort Space Type": "Disk"} for i in range(2)]
        plan = node("Sort", ms=50.0, Sort_Method="external merge", Sort_Space_Used=1200,
                    Sort_Space_Type="Disk", Workers=workers)
        _, result = self.review(plan)
        self.assertEqual(result["findings"][0]["numbers"]["disk_kb"], 3200)
        self.assertIn("across 3 processes", result["findings"][0]["message"])

    def test_multi_batch_hash_is_reported_with_join_time(self):
        plan = node("Hash Join", ms=900.0, plans=[
            node("Seq Scan", ms=100.0, rel="Outer"),
            node("Hash", ms=300.0, rel="Inner", Hash_Batches=16, Original_Hash_Batches=8,
                 Peak_Memory_Usage=4097, plans=[node("Seq Scan", ms=120.0, rel="Outer")]),
        ])
        _, result = self.review(plan)
        finding = result["findings"][0]
        self.assertEqual((finding["kind"], finding["path"]), ("disk-spill", "[0].Plan.Plans[1]"))
        self.assertEqual(finding["numbers"]["hash_batches"], 16)
        # Hash exclusive (300 - 120) plus Hash Join exclusive (900 - 100 - 300)
        self.assertAlmostEqual(finding["numbers"]["time_ms"], 680.0)

    def test_single_batch_hash_is_not_reported(self):
        plan = node("Hash", ms=30.0, Hash_Batches=1, Original_Hash_Batches=1)
        self.assertEqual(self.kinds(plan), [])

    def test_hash_aggregate_disk_usage_is_reported(self):
        plan = node("Aggregate", ms=80.0, Strategy="Hashed", HashAgg_Batches=5, Disk_Usage=26808,
                    Peak_Memory_Usage=4145)
        _, result = self.review(plan)
        self.assertEqual(result["findings"][0]["numbers"]["disk_kb"], 26808)
        self.assertIn("26,808 kB", result["findings"][0]["message"])

    def test_aggregate_without_disk_usage_is_not_reported(self):
        self.assertEqual(self.kinds(node("Aggregate", ms=80.0, Strategy="Hashed", Disk_Usage=0)), [])
        self.assertEqual(self.kinds(node("Aggregate", ms=80.0, Strategy="Sorted")), [])


class EstimateTests(PlanCase):
    def test_under_estimate_is_reported(self):
        _, result = self.review(node("Index Scan", rows=5000, plan_rows=5))
        finding = result["findings"][0]
        self.assertEqual((finding["kind"], finding["severity"]), ("row-misestimate", "medium"))
        self.assertEqual(finding["numbers"]["direction"], "under")

    def test_error_below_factor_is_not_reported(self):
        self.assertEqual(self.kinds(node("Index Scan", rows=9000, plan_rows=1000)), [])
        self.assertEqual(self.kinds(node("Index Scan", rows=9000, plan_rows=1000),
                                    "--estimate-factor", "5"), ["row-misestimate"])

    def test_tiny_row_counts_are_not_reported(self):
        self.assertEqual(self.kinds(node("Index Scan", rows=50, plan_rows=1)), [])

    def test_inherited_error_is_reported_once_at_the_lowest_node(self):
        plan = node("Hash Join", rows=40000, plan_rows=20, plans=[
            node("Seq Scan", rows=40000, plan_rows=20, rel="Outer"),
            node("Hash", rows=10, rel="Inner"),
        ])
        _, result = self.review(plan)
        self.assertEqual([f["path"] for f in result["findings"]], ["[0].Plan.Plans[0]"])

    def test_over_estimate_under_limit_is_not_an_error(self):
        plan = node("Limit", rows=20, plans=[
            node("Index Scan", rows=20, plan_rows=1500000, rel="Outer")])
        self.assertEqual(self.kinds(plan), [])

    def test_over_estimate_under_semi_join_or_merge_join_is_not_an_error(self):
        semi = node("Nested Loop", rows=100, Join_Type="Semi", plans=[
            node("Seq Scan", rows=100, rel="Outer"),
            node("Index Scan", rows=1, plan_rows=4000, loops=100, rel="Inner")])
        merge = node("Merge Join", rows=100, plans=[
            node("Index Scan", rows=100, rel="Outer"),
            node("Index Scan", rows=120, plan_rows=900000, rel="Inner")])
        self.assertEqual(self.kinds(semi), [])
        self.assertEqual(self.kinds(merge), [])
        semi["Join Type"] = "Right Anti"
        self.assertEqual(self.kinds(semi), [])
        semi["Join Type"] = "Inner"
        self.assertEqual(self.kinds(semi), ["row-misestimate"])

    def test_rescanned_inner_side_of_merge_join_is_not_an_under_estimate(self):
        plan = node("Merge Join", rows=90000, plans=[
            node("Index Scan", rows=30000, rel="Outer"),
            node("Sort", rows=90000, plan_rows=300, rel="Inner", plans=[
                node("Seq Scan", rows=300, rel="Outer")])])
        self.assertEqual(self.kinds(plan), [])

    def test_over_estimate_below_a_sort_is_an_error_even_under_limit(self):
        plan = node("Limit", rows=20, plans=[
            node("Sort", rows=20, plan_rows=20, rel="Outer", plans=[
                node("Seq Scan", rows=30, plan_rows=90000, rel="Outer")])])
        _, result = self.review(plan)
        self.assertEqual([(f["kind"], f["path"]) for f in result["findings"]],
                         [("row-misestimate", "[0].Plan.Plans[0].Plans[0]")])
        self.assertEqual(result["findings"][0]["numbers"]["direction"], "over")

    def test_bitmap_and_nodes_are_skipped(self):
        plan = node("Bitmap Heap Scan", rows=5000, plan_rows=5000, plans=[
            node("BitmapAnd", rows=0, plan_rows=5000, rel="Outer")])
        self.assertEqual(self.kinds(plan), [])


class TimingTests(PlanCase):
    def test_exclusive_time_subtracts_children_times_loops(self):
        plan = node("Nested Loop", ms=100.0, plans=[
            node("Seq Scan", ms=10.0, rel="Outer"),
            node("Index Scan", ms=0.5, loops=100, rel="Inner", Index_Name="i"),
        ])
        _, result = self.review(plan)
        hot = dict((h["path"], h["exclusive_ms"]) for h in result["hot_nodes"])
        self.assertEqual(hot, {"[0].Plan": 40.0, "[0].Plan.Plans[0]": 10.0, "[0].Plan.Plans[1]": 50.0})
        self.assertEqual(result["hot_nodes"][0]["path"], "[0].Plan.Plans[1]")

    def test_parallel_nodes_use_average_time_per_process(self):
        plan = node("Gather", rows=10, ms=120.0, plans=[
            scan(removed=333330, rows=3, loops=3, ms=115.0, Parent_Relationship="Outer",
                 Parallel_Aware=True)])
        _, result = self.review(plan)
        finding = result["findings"][0]
        self.assertEqual(finding["node"], "Parallel Seq Scan on t")
        self.assertEqual(finding["numbers"]["rows_removed_by_filter"], 999990)
        self.assertAlmostEqual(finding["numbers"]["time_ms"], 115.0)
        self.assertIn("3 parallel processes", " ".join(finding["evidence"]))
        self.assertIn("Parallel plan", " ".join(result["notes"]))

    def test_single_cte_reader_does_not_keep_the_cte_time(self):
        plan = node("Aggregate", ms=100.0, plans=[
            node("Seq Scan", ms=90.0, rel="InitPlan", Subplan_Name="CTE recent"),
            node("CTE Scan", ms=95.0, rel="Outer", CTE_Name="recent"),
        ])
        _, result = self.review(plan, "--top", "5")
        hot = dict((h["node"], h["exclusive_ms"]) for h in result["hot_nodes"])
        self.assertEqual(hot, {"Seq Scan": 90.0, "CTE Scan on recent": 5.0, "Aggregate": 5.0})
        self.assertEqual(result["notes"], ["No buffer counts in this plan. Capture it with BUFFERS to see I/O."])

    def test_initplan_time_is_flagged_as_double_counted(self):
        plan = node("Seq Scan", ms=50.0, plans=[
            node("Result", ms=30.0, rel="InitPlan", Subplan_Name="InitPlan 1 (returns $0)")])
        _, result = self.review(plan)
        self.assertIn("InitPlan", " ".join(result["notes"]))

    def test_time_outside_plan_nodes_is_noted(self):
        _, result = self.review(node("ModifyTable", ms=10.0, Operation="Delete", Relation_Name="t"),
                                **{"Execution Time": 400.0})
        notes = " ".join(result["notes"])
        self.assertIn("390.000 ms of the 400.000 ms Execution Time is outside the plan nodes", notes)
        self.assertIn("DELETE on t", notes)
        self.assertIn("the statement was executed", notes)


class InputTests(PlanCase):
    def test_psql_framed_output_is_accepted(self):
        body = json.dumps([{"Plan": scan(removed=50000)}], indent=2)
        framed = "                QUERY PLAN\n-------------------------\n"
        framed += "\n".join(" %-60s+" % line for line in body.splitlines()[:-1])
        framed += "\n " + body.splitlines()[-1] + "\n(1 row)\n\n"
        self.path.write_text(framed, encoding="utf-8")
        code, out, _ = run([self.path, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["plans"][0]["findings"][0]["kind"], "seq-scan-filter")

    def test_single_object_with_plan_key_is_accepted(self):
        self.path.write_text(json.dumps({"Query Text": "select 1", "Plan": scan(removed=50000)}),
                             encoding="utf-8")
        code, out, _ = run([self.path, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["plans"][0]["findings"][0]["path"], "Plan")

    def test_two_statements_are_both_reviewed(self):
        self.path.write_text(json.dumps([{"Plan": node("Result")}, {"Plan": scan(removed=50000)}]),
                             encoding="utf-8")
        code, out, _ = run([self.path, "--json"])
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(report["plans"][1]["findings"][0]["path"], "[1].Plan")
        self.assertEqual(report["blocking_findings"], 1)

    def test_bad_inputs_exit_two_with_a_message(self):
        cases = {
            "text plan": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)\n",
            "no plan key": json.dumps([{"Planning Time": 1.0}]),
            "plan is not an object": json.dumps([{"Plan": "Seq Scan"}]),
            "broken json": '[{"Plan": {"Node Type": ',
            "empty": "",
        }
        for label, text in cases.items():
            self.path.write_text(text, encoding="utf-8")
            code, out, err = run([self.path])
            self.assertEqual(code, 2, label)
            self.assertEqual(out, "", label)
            self.assertIn("is not a PostgreSQL JSON plan", err, label)
            self.assertNotIn("Traceback", err, label)

    def test_missing_file_exits_two(self):
        code, _, err = run([Path(self.tmp.name) / "nope.json"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_bad_option_value_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
            plan_review.main([str(PLANS / "healthy.json"), "--estimate-factor", "1"])
        self.assertEqual(caught.exception.code, 2)


class ValidateRepoTests(unittest.TestCase):
    def test_repository_passes_its_own_validator(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import validate
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = validate.main(["validate.py", str(ROOT)])
        self.assertEqual(code, 0, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
