---
name: query-plan-review
description: Reads a saved PostgreSQL EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) plan and a pg_stat_statements export, reports the bottleneck with the plan node and the numbers behind it, and traces the query back to the ORM call that produced it. Use when a query, endpoint or page is slow, when the user pastes an EXPLAIN plan or asks what a plan means, when asked whether to add an index or why an index is not used, when a plan shows a sequential scan, a nested loop or a sort on disk, when a Django, SQLAlchemy, ActiveRecord, Prisma or Hibernate query is slow or repeats per row, or when the user says slow query, query plan, EXPLAIN ANALYZE, pg_stat_statements, N+1 or missing index.
license: MIT
compatibility: Requires Python 3.9 or later to run the bundled scripts. PostgreSQL plans only. No network access, no database connection and no extra packages needed.
metadata:
  author: panhwerwaseem
  version: "0.1.0"
---

# Query plan review

A slow query is diagnosed only when three things are true: the bottleneck is
named from a plan that was executed (ANALYZE) on realistic data, with the plan
node and its numbers; the query is traced to the code that issues it; and the
fix is confirmed by a second plan in which that bottleneck is gone. Everything
below serves those three conditions.

Paths in this file are relative to this skill's directory. The scripts read
saved files. They never connect to a database. PostgreSQL only.

## Hard rules

1. **No index without a plan that shows the need.** Recommend an index only
   when an ANALYZE plan has a `seq-scan-filter` or `index-filter` finding from
   `scripts/plan_review.py` on that table, or a nested loop whose inner side
   scans it sequentially. Quote the node, its path and its row counts in the
   recommendation. "The query is slow" is not evidence that an index is missing.
2. **Measured, on realistic data.** If the script prints `RESULT: NOT MEASURED`
   the plan has estimates only. Do not diagnose from it. Do not trust ANALYZE
   numbers from an empty or tiny development database either: state where the
   plan was captured and how many rows the scans read, and say when that is too
   small to conclude anything.
3. **Ask before executing anything risky.** EXPLAIN ANALYZE runs the statement.
   Never run it on INSERT, UPDATE, DELETE, MERGE, SELECT ... FOR UPDATE, or a
   query that calls a function with side effects, against production, until the
   user has been told it executes the statement and has agreed. Do not run DDL
   (CREATE INDEX, ANALYZE, ALTER) or change settings on a shared or production
   database on your own initiative. Propose the statement instead.
4. **Fix what the plan names.** A row misestimate is fixed with statistics, an
   N+1 in the ORM call, a disk spill by sorting less or allowing more memory.
   Do not answer every slow query with CREATE INDEX.
5. **Candidates are not findings.** `scripts/slow_queries.py` flags N+1
   candidates by shape. A healthy key lookup has the same shape. Find the loop
   in the application code before calling it an N+1 or changing anything.
6. **Verify with a second plan.** After the change, capture the plan the same
   way, run `plan_review.py` on it, and compare the same node and the execution
   time. Do not say "fixed" or quote a speed-up without it. If you cannot
   capture it, report the change as proposed and not verified.

## Workflow

### 1. Collect the evidence

You need the SQL with the parameter values that are slow, an ANALYZE plan of
it, and where that plan was captured. If the user has only ORM code,
`references/orm-tracing.md` shows how to get the SQL from each ORM.

```bash
psql -XAt -d <database> -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <query>" > plan.json
```

For a write statement, use a non-production copy, or with the user's agreement:

```sql
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <write statement>;
ROLLBACK;
```

The rollback undoes the row changes. It does not undo the locks held while the
statement runs, sequence values consumed, or work done by triggers outside the
database. If you have no database access, ask the user to run the command and
save the output.

If nobody knows which statement is slow, do step 2. Otherwise go to step 3.

### 2. Find the statements that cost the most

```bash
psql -X --csv -d <database> -c "SELECT queryid, calls, total_exec_time, mean_exec_time, rows, query FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) ORDER BY total_exec_time DESC LIMIT 200" > pgss.csv
python3 scripts/slow_queries.py pgss.csv
```

On PostgreSQL 12 and earlier the columns are `total_time` and `mean_time`; the
script accepts both. It ranks statements by total time and lists N+1
candidates with the table and lookup column to search for in the code. A
statement with a high mean time needs a plan (step 1). A candidate with a high
call count needs its loop found (step 5).

### 3. Review the plan

```bash
python3 scripts/plan_review.py plan.json
```

Exit code 1 means at least one `high` finding. Each finding gives the node, its
JSON path in the plan file, and the numbers.

| Finding | What the plan shows | Where the fix usually is |
|---|---|---|
| `seq-scan-filter` | The scan read many rows and its filter kept few | An index on the filter columns, if the filter is selective |
| `index-filter` | An index was used, then most fetched rows were discarded | An index that also covers the filter columns |
| `nested-loop-inner`, `subplan-loop` | One side ran once per row of the other | Check the outer row estimate first. Then the inner scan, or rewrite the subquery as a join |
| `disk-spill` | A sort or hash did not fit in memory | Fewer rows or columns, an index that supplies the order, or more `work_mem` for this query |
| `row-misestimate` | The planner's row count was wrong by 10x or more | Statistics: ANALYZE, extended statistics, a predicate the planner can estimate |
| `no-analyze` | Estimates only | Get an ANALYZE plan (step 1) |

A `medium` finding took under 10% of the plan's time, or is a misestimate.
Report it, but it is not what makes this query slow.

### 4. Read the plan around the finding

Open the plan file at the reported path and read the node, its parent and its
children. `references/reading-plans.md` explains every number and lists what
the script cannot judge: a cold cache, a plan that depends on the parameter
value, time spent in triggers, lock waits. When `row-misestimate` and another
finding appear together, the misestimate is usually the cause and the other
finding the symptom.

### 5. Trace the query to the code

Match the table and column names to a model and find the call that builds the
query. `references/orm-tracing.md` has the method for Django, SQLAlchemy,
ActiveRecord, Prisma and Hibernate, and the N+1 fix for each. For an N+1
candidate, find the loop and the per-row access inside it. If you cannot find
one, say so and treat the statement as an ordinary frequent query.

### 6. Choose the fix

Pick the smallest change that removes the named bottleneck.
`references/index-advice.md` covers index shape, column order, the cases where
an index will not help, and how to create one without blocking writes. Write
the fix in the form the codebase uses: a model index plus a migration, or an
eager-loading option on the queryset, not a bare SQL statement pasted into
production.

### 7. Verify

Apply the change where you are allowed to (a development or staging database
with realistic data), then:

```bash
psql -XAt -d <database> -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <same query, same parameters>" > plan-after.json
python3 scripts/plan_review.py plan-after.json
```

The original finding must be gone and the execution time lower. For an N+1,
verification is the number of queries the code path issues (see the test
helpers in `references/orm-tracing.md`), not a plan.

### 8. Report

```
Query:       <statement, and the ORM call that issues it with file:line>
Plan from:   <environment, rows read by the scans, ANALYZE yes or no>
Bottleneck:  <finding kind> at <node> (<JSON path>), <numbers from the script>
Cause:       <one or two sentences>
Fix:         <the code or DDL change, written out>
Verified:    <before and after, execution time and the same node's numbers> or NOT VERIFIED, <why>
Not done:    <DDL not applied, medium findings left, anything the user must run>
```

## Edge cases

- **Text-format plan.** The script reads JSON only. Ask for `FORMAT JSON`. If
  that is not possible, read the plan by hand with
  `references/reading-plans.md` and say that the script was not used.
- **Not PostgreSQL.** The scripts do not read MySQL, SQL Server, SQLite or
  MongoDB plans. Say so. Do not run them on another engine's output.
- **The plan is fast but the page is slow.** Suspect many fast queries, not one
  slow one. Go to step 2, or count the queries the request issues.
- **Fast when you run it, slow in the application.** The application may use
  different parameter values or a cached generic plan. Capture the plan with
  the slow values, and see the parameter section of `references/reading-plans.md`.
- **First run slow, second run fast.** That is a cold cache. Report which run
  the plan came from and its `shared read` buffer count.
- **Plan captured with TIMING OFF.** Row counts are still reliable. The script
  says that it could not weigh findings by time.
- **Queries and plans can hold personal data** in literal values. Do not paste
  them into issues or commit them without checking.
