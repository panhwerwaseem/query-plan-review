# query-plan-review

**An agent skill that reads a real PostgreSQL plan and slow-query statistics, names the bottleneck with the numbers behind it, and traces it back to the ORM call that produced it.**

Left alone, a coding agent asked about a slow query tends to look at the SQL, guess, and recommend `CREATE INDEX`. It can read per-loop numbers as totals, mistake a `LIMIT` that stopped a scan early for stale statistics, and declare the query fixed without measuring again. This skill replaces that with a protocol and two scripts: no index recommendation without a plan node that shows the need, no diagnosis from estimates, no `EXPLAIN ANALYZE` of a write on production without your agreement, and no "fixed" until a second plan shows the bottleneck gone.

PostgreSQL only. The scripts read the JSON that PostgreSQL's `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` prints and CSV exports of `pg_stat_statements`. They do not read plans from MySQL, SQL Server, SQLite or any other engine.

Works with Claude Code, and with any agent that reads the open [Agent Skills](https://agentskills.io) format (Codex, Cursor, GitHub Copilot, Gemini CLI and others).

## What it looks like

A sequential scan that reads 1.5 million rows to return 3:

```console
$ python3 scripts/plan_review.py evals/files/plans/seq-scan.json
plan: evals/files/plans/seq-scan.json
execution time: 187.262 ms   nodes: 3   ANALYZE: yes   BUFFERS: yes

[high] seq-scan-filter  Seq Scan on shop_order
    path: [0].Plan.Plans[0].Plans[0]  (Limit > Sort > Seq Scan)
    read 1,500,000 rows; the filter discarded 1,499,997 and kept 3
    filter: ((external_ref)::text = 'WEB-48213'::text)
    time: 186.921 ms, 99.8% of the plan's 187.221 ms
    buffers: shared hit 2,140, shared read 12,235

Most exclusive time (top 3 of 3 nodes):
     186.921 ms   99.8%  Seq Scan on shop_order  at [0].Plan.Plans[0].Plans[0]
       0.293 ms    0.2%  Sort  at [0].Plan.Plans[0]
       0.007 ms    0.0%  Limit  at [0].Plan

RESULT: 1 high, 0 medium finding(s).
```

A nested loop caused by a row estimate that was wrong by a factor of 2,760. The fix is statistics, not an index:

```console
$ python3 scripts/plan_review.py evals/files/plans/misestimate.json
plan: evals/files/plans/misestimate.json
execution time: 470.938 ms   nodes: 3   ANALYZE: yes   BUFFERS: yes

[high] nested-loop-inner  Nested Loop
    path: [0].Plan  (Nested Loop)
    inner side (Index Scan using orders_pkey on orders o) ran 38,640 times
    each run: 0.011 ms, 1 row(s) on average
    outer side (Index Scan using shipments_warehouse_id_status_idx on shipments s): planner estimated 14 rows, got 38,640
    time: 425.040 ms, 90.8% of the plan's 468.212 ms

[medium] row-misestimate  Index Scan using shipments_warehouse_id_status_idx on shipments s
    path: [0].Plan.Plans[0]  (Nested Loop > Index Scan)
    planner estimated 14 rows, the node returned 38,640 (2,760x under-estimate)
    index cond: ((warehouse_id = 7) AND (status = 'pending'::text))
    feeds: Nested Loop at [0].Plan

Most exclusive time (top 3 of 3 nodes):
     425.040 ms   90.8%  Index Scan using orders_pkey on orders o  at [0].Plan.Plans[1]
      21.665 ms    4.6%  Nested Loop  at [0].Plan
      21.507 ms    4.6%  Index Scan using shipments_warehouse_id_status_idx on shipments s  at [0].Plan.Plans[0]

RESULT: 1 high, 1 medium finding(s).
```

A plan captured without ANALYZE is not diagnosed:

```console
$ python3 scripts/plan_review.py evals/files/plans/estimate-only.json
plan: evals/files/plans/estimate-only.json
nodes: 2   ANALYZE: NO   BUFFERS: no

[medium] no-analyze  ModifyTable on orders
    path: [0].Plan  (ModifyTable)
    estimates only: this plan was captured without ANALYZE, so it has no actual rows, loops or times
    Costs and row counts in it are planner guesses. No bottleneck can be confirmed from this file.

note: This is a plan for UPDATE on orders. EXPLAIN ANALYZE would execute the statement.

RESULT: NOT MEASURED. Get an ANALYZE plan before diagnosing or recommending anything.
```

A plan where a `LIMIT` stopped the scan after 20 of an estimated 1,500,000 rows is not reported as a misestimate:

```console
$ python3 scripts/plan_review.py evals/files/plans/healthy.json
plan: evals/files/plans/healthy.json
execution time: 0.221 ms   nodes: 4   ANALYZE: yes   BUFFERS: yes

Most exclusive time (top 3 of 4 nodes):
       0.120 ms   64.2%  Index Scan using customers_pkey on customers c  at [0].Plan.Plans[0].Plans[1]
       0.043 ms   23.0%  Index Scan using orders_created_at_idx on orders o  at [0].Plan.Plans[0].Plans[0]
       0.018 ms    9.6%  Nested Loop  at [0].Plan.Plans[0]

RESULT: no findings. This plan shows no bottleneck that this script detects.
```

Ranking a `pg_stat_statements` export and flagging the lookup that runs once per row:

```console
$ python3 scripts/slow_queries.py evals/files/stats/pg_stat_statements.csv --top 4 --width 60
file: evals/files/stats/pg_stat_statements.csv
statements: 11   total time: 2,312,168.2 ms   time columns: total_exec_time, mean_exec_time

Top 4 by total time (share is of the statements in this file):
   #   line        total ms   share       calls     mean ms  rows/call  query
   1      2     1,386,028.8   59.9%         288    4812.600       30.0  SELECT date_trunc($1, "shop_order"."created_at") AS day, ...
   2      8       356,396.8   15.4%       1,912     186.400        2.9  SELECT "shop_order"."id", "shop_order"."customer_id", "sh...
   3      9       313,856.0   13.6%       5,120      61.300       20.0  SELECT "shop_product"."id", "shop_product"."sku", "shop_p...
   4     10       200,133.1    8.7%     485,760       0.412        3.0  SELECT "shop_orderitem"."id", "shop_orderitem"."order_id"...

N+1 candidates (SELECT with a parameter equality, calls >= 1000, mean <= 5.0 ms): 3

[high] line 10 (queryid -7304118220194465512): 485,760 calls x 0.412 ms = 200,133.1 ms (8.7% of total time)
    table: shop_orderitem   lookup key: shop_orderitem.order_id   rows per call: 3.0
    SELECT ... FROM "shop_orderitem" WHERE "shop_orderitem"."order_id" = $1

[medium] line 13 (queryid -6120049981174302284): 96,410 calls x 0.031 ms = 2,988.7 ms (0.1% of total time)
    table: django_session   lookup key: django_session.session_key   rows per call: 1.0
    SELECT ... FROM "django_session" WHERE ("django_session"."expire_date" > $1 AND "django_session"....

[medium] line 11 (queryid 7719350014428831202): 24,288 calls x 1.930 ms = 46,875.8 ms (2.0% of total time)
    table: shop_order   lookup key: shop_order.customer_id   rows per call: 20.0
    SELECT ... FROM "shop_order" WHERE "shop_order"."customer_id" = $1 ORDER BY "shop_order"."created...

RESULT: 1 high, 2 medium N+1 candidate(s). Candidates only: find the loop in the application code before changing anything.
```

## Install

Pick one route. Installing through more than one gives you the skill twice.

**Claude Code (plugin)**

```
/plugin install query-plan-review --marketplace panhwerwaseem/query-plan-review
```

On Claude Code versions before 2.1.275, use two steps:

```
/plugin marketplace add panhwerwaseem/query-plan-review
/plugin install query-plan-review@panhwerwaseem-query-plan-review
```

**Any agent (skills CLI)**

```bash
npx skills add panhwerwaseem/query-plan-review
```

**Manual**

Copy the `skills/query-plan-review` folder into `.claude/skills/` (Claude Code) or `.agents/skills/` (cross-agent) in your project, or into the same folder under your home directory.

To pin a release, add the tag: `/plugin marketplace add panhwerwaseem/query-plan-review#v0.1.0` or `npx skills add panhwerwaseem/query-plan-review@v0.1.0`. Third-party plugins do not update automatically; run `claude plugin update query-plan-review@panhwerwaseem-query-plan-review` to update.

## Use

Describe the problem and the skill loads on its own:

> The orders page takes 3 seconds. Here is the EXPLAIN ANALYZE output. Do we need an index?

In Claude Code you can also call it directly with `/query-plan-review`.

Capture a plan for the scripts with:

```bash
psql -XAt -d <database> -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <query>" > plan.json
```

`EXPLAIN ANALYZE` executes the statement. For anything that writes, read [SECURITY.md](SECURITY.md) first.

## What is in the skill

| File | Purpose |
|---|---|
| `SKILL.md` | The protocol: six hard rules and an eight-step workflow from evidence to verified fix |
| `scripts/plan_review.py` | Parses a JSON plan. Reports sequential and index scans whose filter discards most rows, nested-loop inner sides and correlated subplans that run many times, sorts and hashes that spill to disk, row estimates wrong by 10x or more, and plans with no ANALYZE data. Lists the nodes with the most exclusive time. Every finding names the node, its JSON path and its numbers |
| `scripts/slow_queries.py` | Reads a `pg_stat_statements` CSV export, ranks statements by total time, and flags N+1 candidates (many calls, low mean time, a lookup by one key) with the table and key column to search for in the code |
| `references/reading-plans.md` | What each number on a plan node means, what each finding means and how to confirm it, and what the script cannot judge |
| `references/index-advice.md` | Index shape and column order, conditions an index cannot serve, when an index will not help, and creating one without blocking writes |
| `references/orm-tracing.md` | Going from a statement to the code in Django, SQLAlchemy, ActiveRecord, Prisma and Hibernate, and the N+1 fix for each |
| `evals/` | Four plan files with one known bottleneck each, an estimate-only plan, a healthy plan, two `pg_stat_statements` exports, a small Django app that issues the queries, eval cases, and trigger queries |

### How severity is decided

`plan_review.py` multiplies per-loop rows and times by the loop count, then computes each node's exclusive time (its own time minus its children). A finding is `high` unless the plan's timing shows its node took less than 10% of the plan's time; then it is `medium`. A row misestimate is always `medium`, because it explains a problem and does not itself cost time. The exit code is 1 when there is a `high` finding; `--fail-on medium` makes it stricter.

Over-estimates are ignored where a parent could have stopped the node early: under a `Limit`, a `Merge Join`, a semi or anti join, or inside a subplan. Under-estimates are reported at the lowest node where they begin, except on the inner child of a `Merge Join`, whose row count includes re-read rows.

`slow_queries.py` marks an N+1 candidate `high` when it accounts for at least 5% of the total time of the statements in the file. A healthy primary-key lookup has the same shape as an N+1 child query, so these are candidates. The skill requires the agent to find the loop in the code before acting.

### What the scripts do not do

- They do not connect to a database, run `EXPLAIN`, or apply any change.
- They do not read text-format plans or plans from other database engines.
- They do not check incremental sorts for disk use, join filters that discard rows, lossy bitmap scans, or index-only scans with many heap fetches.
- They do not see lock waits, cold-cache effects, or time spent sending rows to the client. `references/reading-plans.md` lists these.
- Exclusive times are approximate in parallel plans and around InitPlans and shared CTEs. The script prints a note when that applies.

## Requirements

- Python 3.9 or later
- No packages to install and no network access
- A plan or statistics export from PostgreSQL. `psql` is the usual way to capture one; the scripts do not need it

## What it reads, writes and contacts

- **Reads:** the plan file or CSV file you pass on the command line.
- **Writes:** nothing. Results go to standard output, errors to standard error.
- **Runs:** nothing. The scripts start no processes and open no database connection.
- **Contacts:** nothing. The scripts contain no network code; CI enforces this.
- **Permissions:** the skill pre-approves no tools. Your agent asks before running commands, as usual.

See [SECURITY.md](SECURITY.md).

## Testing status

- The scripts have unit tests that run in CI on Python 3.9 and 3.12, together with a validator for the skill format and plugin manifests.
- The plan and CSV fixtures are written by hand in PostgreSQL's output format, with numbers chosen to be internally consistent. They were not captured from a live server. If a real plan from your PostgreSQL version is parsed wrongly, please open an issue with the plan attached.
- The eval cases in `skills/query-plan-review/evals/` are in the [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) format. A with-skill versus without-skill score has not been published yet; results are welcome as an issue or pull request.

## Development

```bash
python3 tools/validate.py              # skill format, manifests, no-network check
python3 -m unittest discover -s tests  # script unit tests
```

## License

[MIT](LICENSE)
