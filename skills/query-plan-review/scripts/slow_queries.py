#!/usr/bin/env python3
"""Rank a pg_stat_statements CSV export by total time and flag N+1 candidates.

Input is a CSV file with a header row, exported from the pg_stat_statements
view. Required columns: query, calls, and total_exec_time (PostgreSQL 13 and
later) or total_time (12 and earlier). Optional: mean_exec_time or mean_time,
rows, queryid. Times are milliseconds, as the view reports them.

An N+1 candidate is a statement that
  - is a SELECT with a FROM clause and a WHERE clause that compares a column
    with a parameter (column = $1, or column = ?),
  - was called at least --min-calls times, and
  - has a mean time of at most --max-mean-ms.
That is the shape of a per-row lookup issued from a loop in application code.
It is also the shape of a healthy primary-key lookup, so these are candidates:
confirm each one in the application code before changing anything.

Severity: "high" when the candidate takes at least --min-share of the total
time of all statements in the file, otherwise "medium".

Examples:
  psql -X --csv -c "SELECT queryid, calls, total_exec_time, mean_exec_time, rows, query
      FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 200" > pgss.csv
  slow_queries.py pgss.csv
  slow_queries.py pgss.csv --top 20 --min-calls 5000
  slow_queries.py pgss.csv --json

Exit codes: 0 = no candidate at or above --fail-on, 1 = at least one,
2 = usage or input error.
Standard library only. Reads one file; opens no database connection and makes
no network calls. Writes only to stdout and stderr.
"""
import argparse
import csv
import json
import re
import sys

TOTAL_COLUMNS = ("total_exec_time", "total_time")
MEAN_COLUMNS = ("mean_exec_time", "mean_time")
RANK = {"high": 2, "medium": 1}
LEADING_COMMENTS = r"^\s*(?:(?:/\*.*?\*/|--[^\n]*\n)\s*)*"
SELECT_RE = re.compile(LEADING_COMMENTS + r"select\b", re.I | re.S)
FROM_RE = re.compile(r"\bfrom\s+((?:\"[^\"]+\"|\w+)(?:\.(?:\"[^\"]+\"|\w+))?)", re.I)
KEY_RE = re.compile(r"((?:\"[^\"]+\"|\w+)(?:\.(?:\"[^\"]+\"|\w+))*)\s*=\s*(?:\$\d+|\?)")


class InputError(Exception):
    pass


def to_number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def read_statements(path):
    """Return (statements, time columns used, lines skipped)."""
    csv.field_size_limit(2 ** 31 - 1)  # query texts can be long
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = [name.strip().lower() for name in next(reader)]
        except StopIteration:
            raise InputError("the file is empty")
        total_col = next((c for c in TOTAL_COLUMNS if c in header), None)
        mean_col = next((c for c in MEAN_COLUMNS if c in header), None)
        missing = [c for c in ("query", "calls") if c not in header]
        if total_col is None:
            missing.append("total_exec_time (or total_time)")
        if missing:
            raise InputError(
                "missing column(s): %s. Found: %s. The first line must be the header row of a "
                "pg_stat_statements export." % (", ".join(missing), ", ".join(header) or "(none)"))
        statements, skipped = [], []
        line = reader.line_num + 1
        for record in reader:
            first_line, line = line, reader.line_num + 1
            if not any(cell.strip() for cell in record):
                continue
            row = dict(zip(header, record))
            calls, total = to_number(row.get("calls")), to_number(row.get(total_col))
            if len(record) != len(header) or calls is None or total is None or calls < 0 or total < 0:
                skipped.append(first_line)
                continue
            mean = to_number(row.get(mean_col)) if mean_col else None
            if mean is None:
                mean = total / calls if calls else 0.0
            rows = to_number(row.get("rows")) if "rows" in header else None
            shape = lookup_shape(row["query"])
            statements.append({
                "line": first_line,
                "queryid": (row.get("queryid") or "").strip() or None,
                "calls": int(calls), "total_ms": total, "mean_ms": mean,
                "rows": None if rows is None else int(rows),
                "lookup": None if shape is None else {"table": shape[0], "key_columns": shape[1]},
                "query": " ".join(row["query"].split()),
            })
    if not statements:
        raise InputError("no usable data rows (calls and %s must be numbers)" % total_col)
    return statements, [c for c in (total_col, mean_col) if c], skipped


def strip_quotes(identifier):
    return ".".join(part.strip('"') for part in re.findall(r"\"[^\"]+\"|\w+", identifier))


def lookup_shape(query):
    """Return (table, key columns) if the query is a SELECT ... FROM ... WHERE
    <column> = <parameter>, else None. The names are a search hint for the ORM
    code, taken from the text with regular expressions, not from a SQL parser."""
    if not SELECT_RE.match(query):
        return None
    from_match = FROM_RE.search(query)
    where = re.search(r"\bwhere\b", query, re.I)
    if not from_match or not where or where.start() < from_match.start():
        return None
    keys = []
    for match in KEY_RE.finditer(query, where.end()):
        name = strip_quotes(match.group(1))
        if name not in keys:
            keys.append(name)
    if not keys:
        return None
    return strip_quotes(from_match.group(1)), keys


def analyse(statements, opts):
    total = sum(s["total_ms"] for s in statements)
    ranked = sorted(statements, key=lambda s: (-s["total_ms"], s["line"]))
    for position, statement in enumerate(ranked, 1):
        statement["rank"] = position
        statement["share_of_total_time"] = round(statement["total_ms"] / total, 4) if total else 0.0
        statement["rows_per_call"] = (
            None if statement["rows"] is None or not statement["calls"]
            else round(statement["rows"] / float(statement["calls"]), 2))
    candidates = []
    for statement in ranked:
        if (statement["lookup"] is None or statement["calls"] < opts.min_calls
                or statement["mean_ms"] > opts.max_mean_ms):
            continue
        candidate = dict(statement)
        candidate["severity"] = (
            "high" if statement["share_of_total_time"] >= opts.min_share else "medium")
        candidates.append(candidate)
    candidates.sort(key=lambda c: (-RANK[c["severity"]], -c["calls"], c["line"]))
    return total, ranked, candidates


def clip(text, width):
    return text if len(text) <= width else text[:width - 3] + "..."


def print_text(report, opts):
    print("file: %s" % report["file"])
    print("statements: %d   total time: %s ms   time columns: %s" % (
        report["statement_count"], "{:,.1f}".format(report["total_ms"]),
        ", ".join(report["time_columns"])))
    if report["skipped_lines"]:
        print("skipped %d unreadable row(s) starting at line(s): %s" % (
            len(report["skipped_lines"]), ", ".join(str(n) for n in report["skipped_lines"][:10])))
    top = report["top"]
    print("\nTop %d by total time (share is of the statements in this file):" % len(top))
    print("  %2s  %5s  %14s  %6s  %10s  %10s  %9s  %s" % (
        "#", "line", "total ms", "share", "calls", "mean ms", "rows/call", "query"))
    for s in top:
        print("  %2d  %5d  %14s  %5.1f%%  %10s  %10.3f  %9s  %s" % (
            s["rank"], s["line"], "{:,.1f}".format(s["total_ms"]), s["share_of_total_time"] * 100,
            "{:,}".format(s["calls"]), s["mean_ms"],
            "-" if s["rows_per_call"] is None else "%.1f" % s["rows_per_call"],
            clip(s["query"], opts.width)))
    candidates = report["n_plus_one_candidates"]
    print("\nN+1 candidates (SELECT with a parameter equality, calls >= %d, mean <= %s ms): %d" % (
        opts.min_calls, opts.max_mean_ms, len(candidates)))
    for c in candidates:
        print("\n[%s] line %d%s: %s calls x %.3f ms = %s ms (%.1f%% of total time)" % (
            c["severity"], c["line"], " (queryid %s)" % c["queryid"] if c["queryid"] else "",
            "{:,}".format(c["calls"]), c["mean_ms"], "{:,.1f}".format(c["total_ms"]),
            c["share_of_total_time"] * 100))
        print("    table: %s   lookup key: %s%s" % (
            c["lookup"]["table"], ", ".join(c["lookup"]["key_columns"]),
            "" if c["rows_per_call"] is None else "   rows per call: %.1f" % c["rows_per_call"]))
        start = re.search(r"\bfrom\b", c["query"], re.I).start()  # show the WHERE, not the column list
        shown = c["query"] if start < 40 else "SELECT ... " + c["query"][start:]
        print("    %s" % clip(shown, opts.width + 40))
    high = sum(1 for c in candidates if c["severity"] == "high")
    print("")
    if candidates:
        print("RESULT: %d high, %d medium N+1 candidate(s). Candidates only: find the loop in the "
              "application code before changing anything." % (high, len(candidates) - high))
    else:
        print("RESULT: no N+1 candidates at these thresholds.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file", help="CSV export of pg_stat_statements with a header row")
    parser.add_argument("--top", type=int, default=10, help="statements to list (default 10)")
    parser.add_argument("--min-calls", type=int, default=1000,
                        help="calls at which a statement can be an N+1 candidate (default 1000)")
    parser.add_argument("--max-mean-ms", type=float, default=5.0,
                        help="highest mean time of an N+1 candidate, in ms (default 5)")
    parser.add_argument("--min-share", type=float, default=0.05,
                        help="share of total time at which a candidate is high (default 0.05)")
    parser.add_argument("--fail-on", choices=("high", "medium", "never"), default="high",
                        help="lowest severity that makes the exit code 1 (default high)")
    parser.add_argument("--width", type=int, default=100, help="query text width in text output")
    parser.add_argument("--json", action="store_true", help="print JSON with full query texts")
    opts = parser.parse_args(argv)
    if opts.top < 0 or opts.width < 20 or not 0 <= opts.min_share <= 1:
        parser.error("--top must be at least 0, --width at least 20, --min-share between 0 and 1")

    try:
        statements, columns, skipped = read_statements(opts.csv_file)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        print("slow_queries.py: cannot read %s: %s" % (opts.csv_file, exc), file=sys.stderr)
        return 2
    except InputError as exc:
        print("slow_queries.py: %s: %s" % (opts.csv_file, exc), file=sys.stderr)
        return 2

    total, ranked, candidates = analyse(statements, opts)
    floor = {"high": 2, "medium": 1, "never": 3}[opts.fail_on]
    report = {
        "file": opts.csv_file, "statement_count": len(statements), "total_ms": round(total, 3),
        "time_columns": columns, "skipped_lines": skipped, "top": ranked[:opts.top],
        "n_plus_one_candidates": candidates,
        "blocking_findings": sum(1 for c in candidates if RANK[c["severity"]] >= floor),
    }
    if opts.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text(report, opts)
    return 1 if report["blocking_findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
