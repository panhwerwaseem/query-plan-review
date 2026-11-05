# Reading a PostgreSQL plan

How to read the JSON that `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` prints, what
each finding from `scripts/plan_review.py` means, and what the script cannot
judge. Field names are the JSON keys. Details change between PostgreSQL
versions; when a key described here is missing from your plan, check the
EXPLAIN page of the documentation for your version.

## Contents

- [Capturing a plan](#capturing-a-plan)
- [The numbers on a node](#the-numbers-on-a-node)
- [Where the time went](#where-the-time-went)
- [Estimates against actual rows](#estimates-against-actual-rows)
- [The findings, one by one](#the-findings-one-by-one)
- [What the script cannot judge](#what-the-script-cannot-judge)
- [Node types in brief](#node-types-in-brief)

## Capturing a plan

| Command | Runs the statement | Gives |
|---|---|---|
| `EXPLAIN <statement>` | No | Estimated costs and rows only |
| `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <statement>` | Yes | Estimates, actual rows, loops, times, buffer counts |
| `EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) <statement>` | Yes | Actual rows and loops without per-node times |

- `psql -XAt` prints the JSON without a header, alignment padding or row count.
  `plan_review.py` also accepts psql's default framed output.
- Per-node timing reads the system clock repeatedly, so a statement can take
  longer under ANALYZE than it normally does. `TIMING OFF` skips those clock
  reads and still counts rows.
- Under EXPLAIN ANALYZE the result rows are not sent to the client, so network
  and client time are not in the plan.
- For a write statement, wrap the command in `BEGIN` and `ROLLBACK`. The
  statement still runs: it takes its locks, fires its triggers and consumes
  sequence values.
- To capture plans of slow statements as they happen, the `auto_explain`
  module can log them. Its settings include `auto_explain.log_min_duration`,
  `auto_explain.log_analyze`, `auto_explain.log_buffers` and
  `auto_explain.log_format`. Turning on `log_analyze` adds timing overhead to
  every statement, so agree it with whoever owns the server.

Capture the plan with the parameter values that are slow, on data of realistic
size. A plan from a table with a few hundred rows says nothing about the same
query on millions.

## The numbers on a node

| Key | Meaning |
|---|---|
| `Plan Rows` | Rows the planner expected this node to return, per execution |
| `Actual Rows` | Rows it returned, averaged over its executions |
| `Actual Loops` | How many times the node was executed |
| `Actual Total Time` | Milliseconds per execution, averaged, including child nodes |
| `Actual Startup Time` | Milliseconds until the node returned its first row |
| `Rows Removed by Filter` | Rows read and then discarded by `Filter`, averaged per execution |
| `Shared Hit Blocks` | Blocks found in PostgreSQL's buffer cache |
| `Shared Read Blocks` | Blocks requested from the operating system (disk or OS cache) |
| `Temp Read Blocks`, `Temp Written Blocks` | Temporary-file blocks, used by sorts and hashes that spill |

Three rules prevent most misreadings:

1. **Rows and times are per loop.** Multiply by `Actual Loops` for the total.
   An inner index scan showing 0.011 ms and 38,640 loops took 425 ms.
2. **Averages are rounded.** `Actual Rows` is a whole number in PostgreSQL 17
   and earlier, so `0` with many loops can mean "fewer than one row per loop",
   not "no rows".
3. **Buffer counts are totals and include child nodes.** They are not per
   loop. A block is 8 kB unless the server was built otherwise.

A node with `Actual Loops` 0 was never executed. Its estimates were not tested.

In a parallel plan, nodes below `Gather` or `Gather Merge` report one loop per
participating process, and their rows and times are averages per process.
`Workers Planned` and `Workers Launched` on the Gather node show whether the
workers the planner wanted were available.

## Where the time went

`Actual Total Time` includes the node's children. The time spent in the node
itself (exclusive time) is its total, times loops, minus the same product for
each child. `plan_review.py` prints the nodes with the most exclusive time.
Three cases make that figure approximate, and the script prints a note for
each:

- **Parallel plans.** The per-process average approximates wall-clock time
  only if the processes ran side by side.
- **InitPlans and CTEs.** These run inside whichever node first reads their
  result, so that node's time includes them. For a CTE with a single
  `CTE Scan`, the script subtracts the CTE's time from the scan.
- **Triggers.** Time in AFTER triggers, which includes foreign-key checks, is
  listed in the top-level `Triggers` array and counted in `Execution Time`, but
  belongs to no plan node. A slow foreign-key check on a DELETE shows up there,
  not in the tree. BEFORE trigger time is inside the Insert, Update or Delete
  node.

## Estimates against actual rows

The planner chooses join methods and scan types from `Plan Rows`. When the
estimate is far below the actual count, it tends to choose a nested loop where
a hash join was needed, and to give a sort or hash too little memory. Compare
`Plan Rows` with `Actual Rows` on the same node; both are per loop.

**A low actual count is not always an error.** A node can stop early because
its parent stopped asking for rows:

- under `Limit`, once enough rows were returned;
- under a `Merge Join`, when the other input ran out;
- under a semi or anti join (`Join Type` `Semi` or `Anti`), after the first match;
- inside a subplan for `EXISTS`.

`plan_review.py` ignores over-estimates in those positions, unless a `Sort`,
`Hash` or non-streaming `Aggregate` sits in between, because those read their
whole input.

**A high actual count is not always an error either.** A merge join re-reads
inner rows when the outer side has duplicate keys, and every read is counted.
The script ignores under-estimates on the direct inner child of a `Merge Join`.
All other under-estimates are reported. `BitmapAnd`, `BitmapOr` and
`Bitmap Index Scan` nodes are skipped because their row counts are not
comparable.

The script reports only the lowest node where a misestimate begins. Every node
above it inherits the error.

Common causes, and the fix for each:

| Cause | How to recognise it | Fix |
|---|---|---|
| Stale statistics | The table changed a lot since the last ANALYZE (`pg_stat_user_tables.last_analyze`, `last_autoanalyze`) | `ANALYZE <table>`; review autovacuum settings for that table |
| Correlated columns | Two conditions on the same table, each estimated well alone, badly together | `CREATE STATISTICS <name> (dependencies, ndistinct, mcv) ON <a>, <b> FROM <table>`, then `ANALYZE` |
| Skewed values | Wrong only for some parameter values | Raise the column's statistics target (`ALTER TABLE ... ALTER COLUMN ... SET STATISTICS <n>`), then `ANALYZE` |
| Expression or function on a column | The condition wraps the column in a function or cast | Rewrite the condition on the bare column, or create an expression index and `ANALYZE` (this also collects statistics on the expression) |
| Join estimate wrong, scans right | The join node is the lowest misestimated node | Check the join condition and data types; extended statistics do not cover joins |

Extended statistics exist from PostgreSQL 10 (`dependencies`, `ndistinct`); the
`mcv` kind was added in 12. An index does not fix a misestimate.

## The findings, one by one

### seq-scan-filter

The scan read rows and its `Filter` discarded at least 90% of them. The
message gives rows read, discarded and kept. Before recommending an index:

- Check that the kept share is small for typical parameter values, not only
  for the one captured.
- Read the `Filter` text. A cast or function applied to the column
  (`(id)::numeric = ...`, `lower(email) = ...`) means an existing index may be
  unusable as the condition is written. See `index-advice.md`.
- If the scan is the inner side of a nested loop, the loop count multiplies
  its cost. The index belongs on the join column.

### index-filter

An index or bitmap scan fetched rows through `Index Cond` (or `Recheck Cond`)
and `Filter` then discarded most of them. The index matches only part of the
condition. The usual fix is an index whose columns cover both the index
condition and the filter. A filter that keeps most rows is normal and is not
reported.

### nested-loop-inner and subplan-loop

The inner side of a nested loop, or a correlated subplan, ran at least 1,000
times. The message gives the loop count, the time per run and the total. This
is a symptom. Check in this order:

1. The outer side's estimate, printed with the finding. If the planner
   expected a handful of rows and got thousands, fix the estimate.
2. The inner node. A `Seq Scan` repeated per outer row needs an index on the
   join column. An index scan on the join key is already the cheapest lookup.
3. The query shape. A correlated subquery in the SELECT list or WHERE clause
   runs once per row; a join or a lateral join with aggregation often replaces
   it. ORMs produce these from per-row annotations.

A nested loop with many fast index lookups is the right plan when few outer
rows are expected. It is reported as `high` only when it holds 10% or more of
the plan's time.

### disk-spill

- **Sort.** `Sort Space Type` is `Disk`; `Sort Method` is `external sort` or
  `external merge`. In-memory methods are `quicksort` and `top-N heapsort`.
- **Hash.** `Hash Batches` above 1 means the hash table was split into batches
  kept in temporary files. `Original Hash Batches` is what the planner planned.
- **Hash aggregate** (PostgreSQL 13 and later). `Disk Usage` above 0.

Options, least invasive first: return fewer rows or narrower rows; add an
index that returns rows already in the needed order, so no sort runs; raise
`work_mem` for the session or transaction that runs this query (`SET work_mem`
or `SET LOCAL work_mem`). `work_mem` is a limit per sort or hash node, per
process, so raising it server-wide multiplies across connections. Hash nodes
may use `work_mem` times `hash_mem_multiplier` (PostgreSQL 13 and later).
Incremental sorts report their memory differently and are not checked by the
script.

### row-misestimate

See [Estimates against actual rows](#estimates-against-actual-rows). The
finding names the node the misestimated one feeds, which is usually where the
damage shows.

### no-analyze

The plan has `Plan Rows` and costs but no `Actual` keys. Nothing in it was
measured. Get an ANALYZE plan before saying anything about the cause.

## What the script cannot judge

- **Cold cache.** High `Shared Read Blocks` on the first run and none on the
  second means the time was I/O for data not in the buffer cache. Compare two
  runs before blaming the plan.
- **Parameter-dependent plans.** A prepared statement can switch to a generic
  plan that ignores the parameter values. If the statement is fast when run by
  hand with literals and slow from the application, capture the plan the way
  the application runs it (for example from `auto_explain`).
- **Lock waits.** A statement that waited for a lock is slow in the
  application and can be fast when its plan is captured later. Check
  `pg_stat_activity` (`wait_event_type`) while the statement is slow.
- **Rows returned to the client.** A plan that returns a million rows quickly
  can still make a slow endpoint.
- **Planning time.** A large `Planning Time` against a small `Execution Time`
  is not a plan-shape problem.
- **Whether a faster plan exists.** The script reports where measured time
  went and which known patterns appear. It does not test alternatives.

## Node types in brief

| Node | What it does | Look at |
|---|---|---|
| `Seq Scan` | Reads every row of the table | `Filter`, `Rows Removed by Filter` |
| `Index Scan` | Walks an index, then fetches each row from the table | `Index Cond`, `Filter` |
| `Index Only Scan` | Answers from the index when the visibility map allows | `Heap Fetches`; a high count means the table needs VACUUM for the scan to stay index-only |
| `Bitmap Index Scan` + `Bitmap Heap Scan` | Collects matching row locations, then reads table pages in order | `Recheck Cond`, `Lossy Heap Blocks` |
| `Nested Loop` | Runs the inner side once per outer row | Inner `Actual Loops` |
| `Hash Join` + `Hash` | Builds a hash table from one input, probes it with the other | `Hash Batches` |
| `Merge Join` | Merges two inputs sorted on the join key | Sorts below it |
| `Sort` | Sorts its whole input | `Sort Method`, `Sort Space Type` |
| `Aggregate` | Groups rows; `Strategy` is `Plain`, `Sorted`, `Hashed` or `Mixed` | `Disk Usage` for hashed strategies |
| `Gather`, `Gather Merge` | Collects rows from parallel workers | `Workers Launched` |
| `Limit` | Stops after N rows | Whether a `Sort` below it had to read everything first |
| `Memoize` (PostgreSQL 14 and later) | Caches inner results of a nested loop per key | `Cache Hits`, `Cache Misses` |
| `CTE Scan` | Reads the result of a WITH query that was computed once | The node under the matching `Subplan Name` |
