#!/usr/bin/env python3
"""Review a saved PostgreSQL EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) plan.

Reads one plan file and reports each finding with the plan node, its JSON path,
and the numbers behind it:

  seq-scan-filter    sequential scan whose filter discards most of the rows it reads
  index-filter       index or bitmap heap scan whose filter discards most fetched rows
  nested-loop-inner  nested loop whose inner side ran many times
  subplan-loop       correlated subplan that ran many times
  disk-spill         sort, hash or hash aggregate that spilled to temp files
  row-misestimate    row estimate wrong by 10x or more (the lowest node where it starts)
  no-analyze         the plan has estimates only (EXPLAIN without ANALYZE)

It also lists the nodes with the most exclusive time (time x loops, minus children).

Severity: a finding is "high" unless the plan's timing shows that its node took
less than --min-share of the plan's time; then it is "medium". row-misestimate
and no-analyze are always "medium": they explain a problem, they do not cost time.

Examples:
  psql -XAt -f query.sql -o plan.json   # query.sql: EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT ...
  plan_review.py plan.json
  plan_review.py plan.json --json
  plan_review.py plan.json --min-loops 500 --fail-on medium

Exit codes: 0 = no finding at or above --fail-on, 1 = at least one such finding,
2 = usage or input error.
PostgreSQL only. Standard library only. Reads one file; opens no database
connection and makes no network calls. Writes only to stdout and stderr.
"""
import argparse
import json
import re
import sys

DISCARD_FRACTION = 0.9  # "most rows": the filter removed at least 90% of the rows read
FILTER_SCANS = ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan")
NO_ROW_COUNT = ("BitmapAnd", "BitmapOr", "Bitmap Index Scan")  # row counts are not comparable
RANK = {"high": 2, "medium": 1}
BUFFER_KEYS = (("Shared Hit Blocks", "shared hit"), ("Shared Read Blocks", "shared read"),
               ("Temp Written Blocks", "temp written"))


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def fmt(value):
    """1499958 -> '1,499,958'. Fractional row counts keep one decimal."""
    if isinstance(value, float) and value != int(value):
        return "{:,.1f}".format(value)
    return "{:,}".format(int(value))


class Node(object):
    def __init__(self, raw, path, parent):
        self.raw, self.path, self.parent = raw, path, parent
        self.children = []
        self.type = str(raw.get("Node Type") or "?")
        self.rel = raw.get("Parent Relationship")
        self.loops = number(raw.get("Actual Loops"))
        self.rows = number(raw.get("Actual Rows"))
        self.loop_ms = number(raw.get("Actual Total Time"))
        self.procs = 1      # processes sharing this node's work under a Gather
        self.incl = None    # wall-clock ms including children
        self.excl = None    # wall-clock ms excluding children
        self.note = None
        label = ("Parallel " if raw.get("Parallel Aware") else "") + self.type
        if raw.get("Index Name"):
            label += " using %s" % raw["Index Name"]
        if raw.get("Relation Name"):
            label += " on %s" % raw["Relation Name"]
            if raw.get("Alias") and raw["Alias"] != raw["Relation Name"]:
                label += " %s" % raw["Alias"]
        elif raw.get("CTE Name"):
            label += " on %s" % raw["CTE Name"]
        self.label = label

    def lineage(self):
        names, node = [], self
        while node is not None:
            names.append(node.type)
            node = node.parent
        return " > ".join(reversed(names))


def build(raw, path, parent, nodes):
    node = Node(raw, path, parent)
    nodes.append(node)
    for index, child in enumerate(raw.get("Plans") or []):
        if isinstance(child, dict):
            node.children.append(build(child, "%s.Plans[%d]" % (path, index), node, nodes))
    return node


def compute_times(nodes):
    """Fill incl and excl. Actual Total Time is a per-loop average, so the node's
    total is time x loops. Under a Gather the loops are spread over parallel
    processes; dividing by the process count approximates wall-clock time."""
    for node in nodes:  # parents come before children
        parent = node.parent
        if parent is not None:
            if parent.type in ("Gather", "Gather Merge") and node.rel != "InitPlan":
                node.procs = max(int(node.loops or 1), 1)
            else:
                node.procs = parent.procs
        if node.loop_ms is not None and node.loops is not None:
            node.incl = node.loop_ms * node.loops / node.procs
    for node in nodes:
        if node.incl is None:
            continue
        # An InitPlan (including a CTE) runs inside whichever node first reads
        # its result, so its time is not subtracted from the node it hangs on.
        below = sum(c.incl for c in node.children if c.rel != "InitPlan" and c.incl is not None)
        node.excl = max(node.incl - below, 0.0)
    ctes = dict((n.raw.get("Subplan Name"), n) for n in nodes if n.rel == "InitPlan")
    readers = {}
    for node in nodes:
        if node.type == "CTE Scan" and node.raw.get("CTE Name"):
            readers.setdefault("CTE %s" % node.raw["CTE Name"], []).append(node)
    for name in sorted(readers):
        cte, scans = ctes.get(name), readers[name]
        if cte is None or cte.incl is None:
            continue
        if len(scans) == 1 and scans[0].excl is not None:
            scans[0].excl = max(scans[0].excl - cte.incl, 0.0)
        else:
            for scan in scans:
                scan.note = "includes part of the time spent computing %s" % name


def estimate_error(node):
    """Return (factor, 'under' or 'over') comparing Plan Rows with Actual Rows.
    Both are per-loop values. Zero counts as one row: the planner never
    estimates fewer, and an actual 0 can be a rounded per-loop average."""
    plan = number(node.raw.get("Plan Rows"))
    if plan is None or node.rows is None or not node.loops or node.type in NO_ROW_COUNT:
        return 1.0, None
    plan, actual = max(plan, 1.0), max(node.rows, 1.0)
    if actual >= plan:
        return actual / plan, "under"
    return plan / actual, "over"


def may_stop_early(node):
    """True when something above this node may have stopped reading it before
    it returned all its rows. Then a low actual row count is not an estimate error."""
    current = node
    while current.parent is not None:
        if current.rel in ("InitPlan", "SubPlan"):
            return True
        parent = current.parent
        if parent.type in ("Sort", "Hash") or (
                parent.type == "Aggregate" and parent.raw.get("Strategy") in ("Plain", "Hashed")):
            return False  # these read their whole input
        join_type = str(parent.raw.get("Join Type") or "")  # also "Right Semi", "Right Anti"
        if parent.type in ("Limit", "Merge Join") or "Semi" in join_type or "Anti" in join_type:
            return True
        current = parent
    return False


class Review(object):
    def __init__(self, opts, total_ms, timed):
        self.opts, self.total_ms, self.timed = opts, total_ms, timed
        self.findings = []

    def share(self, ms):
        if not self.timed or ms is None:
            return None
        return ms / self.total_ms if self.total_ms else 0.0

    def time_line(self, ms, procs=1):
        share = self.share(ms)
        if share is None:
            return "time: not collected%s" % ("" if self.timed else " (plan captured with TIMING OFF)")
        text = "time: %.3f ms, %.1f%% of the plan's %.3f ms" % (ms, share * 100, self.total_ms)
        if procs > 1:
            text += " (average per process, %d parallel processes)" % procs
        return text

    def add(self, kind, node, message, evidence, numbers, ms=None, severity=None):
        share = self.share(ms)
        if severity is None:
            severity = "medium" if share is not None and share < self.opts.min_share else "high"
        numbers = dict(numbers)
        numbers["time_ms"] = None if share is None else round(ms, 3)
        numbers["share_of_plan_time"] = None if share is None else round(share, 4)
        self.findings.append({
            "severity": severity, "kind": kind, "node": node.label, "path": node.path,
            "lineage": node.lineage(), "message": message, "evidence": evidence,
            "numbers": numbers,
        })


def buffers_line(node):
    parts = ["%s %s" % (name, fmt(node.raw[key])) for key, name in BUFFER_KEYS if node.raw.get(key)]
    return ["buffers: " + ", ".join(parts)] if parts else []


def check_filter(node, review):
    removed = number(node.raw.get("Rows Removed by Filter"))
    if node.type not in FILTER_SCANS or not removed or not node.loops:
        return
    removed_total = removed * node.loops
    kept = (node.rows or 0) * node.loops
    read = removed_total + kept
    if removed_total < review.opts.min_rows or removed_total / float(read) < DISCARD_FRACTION:
        return
    seq = node.type == "Seq Scan"
    message = "%s %s rows; the filter discarded %s and kept %s" % (
        "read" if seq else "fetched", fmt(read), fmt(removed_total), fmt(kept))
    if node.loops > 1:
        message += " (over %s loops, from per-loop averages)" % fmt(node.loops)
    evidence = ["filter: %s" % node.raw.get("Filter", "(not shown)")]
    if node.raw.get("Index Cond") or node.raw.get("Recheck Cond"):
        evidence.append("index condition: %s" % (node.raw.get("Index Cond") or node.raw.get("Recheck Cond")))
    evidence += [review.time_line(node.incl, node.procs)] + buffers_line(node)
    review.add("seq-scan-filter" if seq else "index-filter", node, message, evidence,
               {"rows_read": read, "rows_removed_by_filter": removed_total, "rows_kept": kept,
                "loops": node.loops}, node.incl)


def check_loops(node, review):
    repeated = []
    if node.type == "Nested Loop":
        repeated += [("nested-loop-inner", c) for c in node.children if c.rel == "Inner"]
    repeated += [("subplan-loop", c) for c in node.children if c.rel == "SubPlan"]
    for kind, child in repeated:
        if not child.loops or child.loops < review.opts.min_loops:
            continue
        what = "inner side" if kind == "nested-loop-inner" else "correlated subplan"
        message = "%s (%s) ran %s times" % (what, child.label, fmt(child.loops))
        evidence = []
        if child.loop_ms is not None:
            evidence.append("each run: %.3f ms, %s row(s) on average" % (child.loop_ms, fmt(child.rows or 0)))
        numbers = {"loops": child.loops, "ms_per_loop": child.loop_ms}
        outer = [c for c in node.children if c.rel == "Outer"]
        if kind == "nested-loop-inner" and outer and outer[0].loops:
            plan = number(outer[0].raw.get("Plan Rows"))
            if plan is not None and outer[0].rows is not None:
                evidence.append("outer side (%s): planner estimated %s rows, got %s" % (
                    outer[0].label, fmt(plan), fmt(outer[0].rows)))
                numbers.update({"outer_plan_rows": plan, "outer_actual_rows": outer[0].rows})
        evidence.append(review.time_line(child.incl, child.procs))
        review.add(kind, node, message, evidence, numbers, child.incl)


def check_spill(node, review):
    raw, ms = node.raw, node.excl
    # In a parallel plan each worker reports its own sort or aggregate figures.
    entries = [raw] + [w for w in raw.get("Workers") or [] if isinstance(w, dict)]
    if node.type == "Sort":
        spills = [e for e in entries if e.get("Sort Space Type") == "Disk"]
        if not spills:
            return
        kb = sum(number(e.get("Sort Space Used")) or 0 for e in spills)
        message = "sort spilled to disk: %s, %s kB of temp files" % (
            spills[0].get("Sort Method", "external sort"), fmt(kb))
        evidence = ["sort key: %s" % ", ".join(str(k) for k in raw.get("Sort Key") or [])]
        numbers = {"disk_kb": kb, "rows_sorted": (node.rows or 0) * node.loops}
    elif node.type == "Hash" and (number(raw.get("Hash Batches")) or 1) > 1:
        spills = [raw]
        message = "hash table did not fit in memory and was split into %s batches on disk" % fmt(
            raw["Hash Batches"])
        evidence = ["planned batches: %s; peak memory: %s kB" % (
            raw.get("Original Hash Batches", "?"), raw.get("Peak Memory Usage", "?"))]
        numbers = {"hash_batches": raw["Hash Batches"],
                   "original_hash_batches": raw.get("Original Hash Batches"),
                   "peak_memory_kb": raw.get("Peak Memory Usage")}
        if node.parent is not None and node.parent.type == "Hash Join" and ms is not None:
            ms += node.parent.excl or 0.0  # building and probing are one operation
    elif node.type == "Aggregate":
        spills = [e for e in entries if (number(e.get("Disk Usage")) or 0) > 0]
        if not spills:
            return
        kb = sum(e["Disk Usage"] for e in spills)
        message = "hash aggregate spilled to disk: %s kB of temp files" % fmt(kb)
        evidence = ["batches: %s; peak memory: %s kB" % (
            spills[0].get("HashAgg Batches", "?"), spills[0].get("Peak Memory Usage", "?"))]
        numbers = {"disk_kb": kb, "peak_memory_kb": spills[0].get("Peak Memory Usage")}
    else:
        return
    if len(spills) > 1:
        message += " across %d processes" % len(spills)
    evidence += [review.time_line(ms, node.procs)] + buffers_line(node)
    review.add("disk-spill", node, message, evidence, numbers, ms)


def check_estimate(node, review):
    factor, direction = estimate_error(node)
    if factor < review.opts.estimate_factor:
        return
    for child in node.children:  # report only where the error starts
        child_factor, child_direction = estimate_error(child)
        if (child.rel not in ("InitPlan", "SubPlan") and child_direction == direction
                and child_factor >= review.opts.estimate_factor):
            return
    if direction == "over" and may_stop_early(node):
        return
    if (direction == "under" and node.rel == "Inner" and node.parent is not None
            and node.parent.type == "Merge Join"):
        return  # a merge join re-reads inner rows for duplicate keys; each read is counted
    plan = number(node.raw.get("Plan Rows"))
    if max(plan, node.rows) * node.loops < review.opts.min_rows:
        return
    message = "planner estimated %s rows, the node returned %s (%sx %s-estimate)" % (
        fmt(plan), fmt(node.rows), fmt(round(factor)), direction)
    if node.loops > 1:
        message += ", per loop over %s loops" % fmt(node.loops)
    evidence = []
    for key in ("Index Cond", "Recheck Cond", "Filter", "Hash Cond", "Join Filter"):
        if node.raw.get(key):
            evidence.append("%s: %s" % (key.lower(), node.raw[key]))
    if node.parent is not None:
        evidence.append("feeds: %s at %s" % (node.parent.label, node.parent.path))
    review.add("row-misestimate", node, message, evidence,
               {"plan_rows": plan, "actual_rows": node.rows, "loops": node.loops,
                "factor": round(factor, 1), "direction": direction}, severity="medium")


def review_plan(doc, prefix, opts):
    nodes = []
    root = build(doc["Plan"], prefix + "Plan", None, nodes)
    analyzed = root.loops is not None
    timed = root.loop_ms is not None
    result = {
        "analyzed": analyzed, "timed": timed, "node_count": len(nodes),
        "buffers": any("Shared Hit Blocks" in n.raw for n in nodes),
        "execution_time_ms": number(doc.get("Execution Time")),
        "planning_time_ms": number(doc.get("Planning Time")),
        "plan_time_ms": None, "findings": [], "hot_nodes": [], "notes": [],
    }
    notes = result["notes"]
    if root.type == "ModifyTable":
        operation = str(root.raw.get("Operation", "write")).upper()
        target = root.raw.get("Relation Name", "a table")
        notes.append(
            "This is a plan for %s on %s. %s" % (operation, target, (
                "It was captured with ANALYZE, so the statement was executed."
                if analyzed else "EXPLAIN ANALYZE would execute the statement.")))
    if not analyzed:
        review = Review(opts, None, False)
        review.add("no-analyze", root,
                   "estimates only: this plan was captured without ANALYZE, so it has no actual "
                   "rows, loops or times",
                   ["Costs and row counts in it are planner guesses. No bottleneck can be "
                    "confirmed from this file."], {}, severity="medium")
        result["findings"] = review.findings
        return result

    compute_times(nodes)
    review = Review(opts, root.incl, timed)
    result["plan_time_ms"] = root.incl
    for node in nodes:
        if not node.loops:
            continue  # never executed
        check_filter(node, review)
        check_loops(node, review)
        check_spill(node, review)
        check_estimate(node, review)
    review.findings.sort(key=lambda f: (-RANK[f["severity"]], -(f["numbers"]["time_ms"] or 0), f["path"]))
    result["findings"] = review.findings

    if timed:
        ranked = sorted((n for n in nodes if n.excl is not None), key=lambda n: (-n.excl, n.path))
        for node in ranked[:opts.top]:
            result["hot_nodes"].append({
                "node": node.label, "path": node.path, "exclusive_ms": round(node.excl, 3),
                "share_of_plan_time": round(review.share(node.excl), 4), "note": node.note})
    else:
        notes.append("No per-node timing in this plan (TIMING OFF). Findings cannot be weighed "
                     "by time, so none was lowered to medium.")
    if not result["buffers"]:
        notes.append("No buffer counts in this plan. Capture it with BUFFERS to see I/O.")
    if any(n.procs > 1 for n in nodes):
        notes.append("Parallel plan: times of nodes under Gather are averages per process "
                     "(time x loops / processes), so their shares are approximate.")
    if any(n.rel == "InitPlan" and not str(n.raw.get("Subplan Name", "")).startswith("CTE ")
           for n in nodes) or any(n.note for n in nodes):
        notes.append("InitPlan or shared CTE time is also counted inside the node that first "
                     "reads its result, so that node's exclusive time is overstated.")
    execution = result["execution_time_ms"]
    if timed and execution and execution - root.incl >= max(opts.min_share * execution, 1.0):
        notes.append("%.3f ms of the %.3f ms Execution Time is outside the plan nodes. Check the "
                     "Triggers and JIT sections of the plan file." % (execution - root.incl, execution))
    return result


def load_plans(text):
    """Return [(path prefix, plan document)]. Accepts the JSON array EXPLAIN prints,
    a single object with a "Plan" key (auto_explain), and psql's framed output."""
    try:
        doc = json.loads(text)
    except ValueError:
        lines = [re.sub(r"\s*\+$", "", line.rstrip()) for line in text.splitlines()]
        body = "\n".join(lines)
        starts = [i for i in (body.find("["), body.find("{")) if i >= 0]
        if not starts:
            raise ValueError("no JSON found; capture the plan with FORMAT JSON")
        doc = json.loads(body[min(starts):max(body.rfind("]"), body.rfind("}")) + 1])
    if isinstance(doc, dict):
        docs = [("", doc)]
    elif isinstance(doc, list):
        docs = [("[%d]." % i, d) for i, d in enumerate(doc)]
    else:
        docs = []
    plans = [(p, d) for p, d in docs if isinstance(d, dict) and isinstance(d.get("Plan"), dict)]
    if not plans or len(plans) != len(docs):
        raise ValueError('expected EXPLAIN (FORMAT JSON) output: an array of objects with a "Plan" key')
    return plans


def print_text(report):
    print("plan: %s" % report["file"])
    for index, plan in enumerate(report["plans"]):
        if len(report["plans"]) > 1:
            print("\n== statement %d of %d ==" % (index + 1, len(report["plans"])))
        head = "nodes: %d   ANALYZE: %s   BUFFERS: %s" % (
            plan["node_count"], "yes" if plan["analyzed"] else "NO", "yes" if plan["buffers"] else "no")
        if plan["execution_time_ms"] is not None:
            head = "execution time: %.3f ms   %s" % (plan["execution_time_ms"], head)
        print(head)
        for finding in plan["findings"]:
            print("\n[%s] %s  %s" % (finding["severity"], finding["kind"], finding["node"]))
            print("    path: %s  (%s)" % (finding["path"], finding["lineage"]))
            print("    %s" % finding["message"])
            for line in finding["evidence"]:
                print("    %s" % line)
        if plan["hot_nodes"]:
            print("\nMost exclusive time (top %d of %d nodes):" % (len(plan["hot_nodes"]), plan["node_count"]))
            for hot in plan["hot_nodes"]:
                print("  %10.3f ms  %5.1f%%  %s  at %s%s" % (
                    hot["exclusive_ms"], hot["share_of_plan_time"] * 100, hot["node"], hot["path"],
                    "  [%s]" % hot["note"] if hot["note"] else ""))
        if plan["notes"]:
            print("")
        for note in plan["notes"]:
            print("note: %s" % note)
    findings = [f for plan in report["plans"] for f in plan["findings"]]
    high = sum(1 for f in findings if f["severity"] == "high")
    print("")
    if not all(plan["analyzed"] for plan in report["plans"]):
        print("RESULT: NOT MEASURED. Get an ANALYZE plan before diagnosing or recommending anything.")
    elif not findings:
        print("RESULT: no findings. This plan shows no bottleneck that this script detects.")
    else:
        print("RESULT: %d high, %d medium finding(s)." % (high, len(findings) - high))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan", help="file holding EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) output")
    parser.add_argument("--min-rows", type=int, default=1000,
                        help="ignore filters that removed, and estimates that involve, fewer rows (default 1000)")
    parser.add_argument("--min-loops", type=int, default=1000,
                        help="loop count at which a nested-loop inner side or subplan is reported (default 1000)")
    parser.add_argument("--estimate-factor", type=float, default=10.0,
                        help="estimate error factor that is reported (default 10)")
    parser.add_argument("--min-share", type=float, default=0.10,
                        help="share of plan time below which a finding is medium, not high (default 0.10)")
    parser.add_argument("--top", type=int, default=3, help="how many hot nodes to list (default 3)")
    parser.add_argument("--fail-on", choices=("high", "medium", "never"), default="high",
                        help="lowest severity that makes the exit code 1 (default high)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    opts = parser.parse_args(argv)
    if opts.estimate_factor <= 1 or not 0 <= opts.min_share <= 1 or opts.top < 0:
        parser.error("--estimate-factor must be above 1, --min-share between 0 and 1, --top at least 0")

    try:
        with open(opts.plan, "r", encoding="utf-8-sig") as handle:
            plans = load_plans(handle.read())
        report = {"file": opts.plan, "plans": [review_plan(doc, prefix, opts) for prefix, doc in plans]}
    except (OSError, UnicodeDecodeError) as exc:
        print("plan_review.py: cannot read %s: %s" % (opts.plan, exc), file=sys.stderr)
        return 2
    except (ValueError, AttributeError, KeyError, TypeError, RecursionError) as exc:
        print("plan_review.py: %s is not a PostgreSQL JSON plan (%s: %s)\nCapture one with: psql -XAt "
              "-c \"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <query>\" > plan.json"
              % (opts.plan, type(exc).__name__, exc), file=sys.stderr)
        return 2
    floor = {"high": 2, "medium": 1, "never": 3}[opts.fail_on]
    report["blocking_findings"] = sum(
        1 for plan in report["plans"] for f in plan["findings"] if RANK[f["severity"]] >= floor)
    if opts.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text(report)
    return 1 if report["blocking_findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
