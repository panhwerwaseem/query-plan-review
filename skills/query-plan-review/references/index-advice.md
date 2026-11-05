# Index advice

When an index is the right fix for a plan finding, what shape it should have,
and when it will not help. PostgreSQL only. Behaviour differs between versions
in places; where this file names a version, check the documentation for yours.

## Contents

- [Before recommending an index](#before-recommending-an-index)
- [Choosing the shape](#choosing-the-shape)
- [Conditions an index cannot serve as written](#conditions-an-index-cannot-serve-as-written)
- [When an index will not help](#when-an-index-will-not-help)
- [What an index costs](#what-an-index-costs)
- [Creating it without blocking writes](#creating-it-without-blocking-writes)
- [Verifying](#verifying)

## Before recommending an index

All of these must hold:

1. An ANALYZE plan shows a scan discarding most of what it reads
   (`seq-scan-filter` or `index-filter` from `scripts/plan_review.py`), or a
   repeated inner scan with no usable index on the join column.
2. That node holds a meaningful share of the plan's time. The script marks the
   finding `high` when it does.
3. The row estimate on that node is roughly right. If it is wrong by 10x or
   more, fix the statistics first; the planner may already have a usable index
   and be ignoring it for a bad reason.
4. No existing index already starts with the same columns. List the table's
   indexes (`\d <table>` in psql, or the ORM model and migrations) before
   proposing a new one.

## Choosing the shape

| Query shape | Index |
|---|---|
| `WHERE a = ?` | `(a)` |
| `WHERE a = ? AND b = ?` | `(a, b)`; put the column that other queries also filter on first |
| `WHERE a = ? AND b > ?` | `(a, b)`: equality columns first, then the range column |
| `WHERE a = ? ORDER BY b LIMIT n` | `(a, b)`: the index returns rows in order, so no sort runs and the scan stops after n rows |
| Join `ON child.parent_id = parent.id` | `(parent_id)` on the child table |
| Condition always includes a fixed value, such as `status = 'open'` | Partial index: `CREATE INDEX ... ON t (assignee_id) WHERE status = 'open'` |
| Condition on an expression, such as `lower(email) = ?` | Expression index: `CREATE INDEX ... ON t (lower(email))` |
| Query reads only a few columns | Covering index: `CREATE INDEX ... ON t (a) INCLUDE (b)` (PostgreSQL 11 and later) |

Notes:

- A multicolumn B-tree index is most useful when the query constrains its
  leading column. An index on `(a, b)` serves `WHERE b = ?` poorly, if at all.
- For an `index-filter` finding, the new index normally extends the one in the
  plan: its `Index Cond` columns plus the `Filter` columns.
- A partial index is used only when the planner can prove the query's
  condition implies the index predicate. Check the second plan to see that it
  was used.
- An `Index Only Scan` avoids the table only for pages marked all-visible.
  `Heap Fetches` in the plan shows how many rows still needed the table. A
  recently written table needs VACUUM before the scan is truly index-only.
- Postgres does not index foreign-key columns automatically. The referenced
  side has its primary key; the referencing column has an index only if
  someone created one. Some ORMs create it for you (Django does for
  `ForeignKey`).

## Conditions an index cannot serve as written

Read the `Filter` text in the plan. The planner prints the casts and functions
it applied.

| Filter in the plan | Problem | Fix |
|---|---|---|
| `(lower((email)::text) = '...')` | Function on the column | Expression index on `lower(email)`, or store the value normalised |
| `((id)::numeric = 42.0)` | The column was cast to match the value's type | Pass a value of the column's type from the application |
| `(date(created_at) = '2026-01-01'::date)` | Function on the column | Rewrite as a range: `created_at >= '2026-01-01' AND created_at < '2026-01-02'` |
| `(name ~~ '%term%'::text)` or `~~*` (LIKE, ILIKE with a leading wildcard) | A B-tree cannot search the middle of a string | A trigram index from the `pg_trgm` extension, or full-text search |
| `(name ~~ 'term%'::text)` with no index use | Prefix search under a non-C collation | An index with the `text_pattern_ops` (or `varchar_pattern_ops`) operator class |
| `((a = 1) OR (b = 2))` | One index cannot serve both branches | An index per branch (the planner can combine them with a bitmap OR), or UNION of two queries |

`((external_ref)::text = 'x'::text)` on a `varchar` column is not a problem.
That cast is how PostgreSQL always compares `varchar`, and an ordinary index
on the column serves it.

## When an index will not help

| Plan finding or situation | Why not | What to do |
|---|---|---|
| `row-misestimate` | The planner chose badly from wrong row counts | ANALYZE, extended statistics, see `reading-plans.md` |
| N+1 candidate with a fast mean time | Each lookup is already fast; the cost is the number of round trips | Fix the loop in the ORM call, see `orm-tracing.md` |
| `disk-spill` on a sort of most of the table | The rows must still be read and sorted or returned | Fewer rows, or `work_mem` for that query. An index helps only when it supplies the order and the query reads a small part of it |
| The filter keeps a large share of the table | Reading most of the table through an index is slower than a sequential scan, and the planner knows it | Return fewer rows, paginate, or aggregate |
| The table is small | A few pages are read faster sequentially | Nothing |
| Time is in triggers, lock waits or the network | Not a scan problem | Find the trigger, the blocking session, or the result size |
| An index exists and the estimate says it is not worth using | Possibly correct, possibly cost settings that do not match the storage | Compare actual times before touching `random_page_cost`; that is a server-wide decision for the database owner |

There is no fixed selectivity threshold at which an index wins. It depends on
row width, how the rows are ordered on disk, and the cost settings. Use the
plan, not a rule of thumb.

## What an index costs

- Every INSERT, and every UPDATE that changes an indexed column or cannot put
  the new row version on the same page, also writes to the index. VACUUM has
  more dead index entries to clean up.
- Disk space and cache space that other data could use.
- Longer planning when a table has many indexes.

Prefer extending or replacing an existing index over adding a near-duplicate.
`pg_stat_user_indexes.idx_scan` shows how often each index has been used since
statistics were last reset.

## Creating it without blocking writes

- Plain `CREATE INDEX` blocks inserts, updates and deletes on the table until
  it finishes. Reads continue.
- `CREATE INDEX CONCURRENTLY` does not block writes. It takes longer, cannot
  run inside a transaction block, and if it fails it leaves an invalid index
  behind that must be dropped before retrying.
- Migration tools usually wrap each migration in a transaction, so a
  concurrent build needs the tool's opt-out:

| Tool | How |
|---|---|
| Django | `AddIndexConcurrently` from `django.contrib.postgres.operations`, in a migration with `atomic = False` |
| Rails | `disable_ddl_transaction!` in the migration, and `add_index ..., algorithm: :concurrently` |
| Alembic | `op.create_index(..., postgresql_concurrently=True)` inside `with op.get_context().autocommit_block():` |
| Others | Check the tool's documentation for running one statement outside a transaction |

Define the index in the ORM model as well, so the schema and the code agree.
After creating an expression index, run `ANALYZE` on the table so the planner
has statistics for the expression.

Creating an index on a production table is the user's decision. Propose the
statement or migration; do not run it.

## Verifying

Capture the plan again with the same statement and parameters and run
`plan_review.py` on it. All of these should be true:

- The original finding is gone.
- The node now names the new index (`Index Name`).
- `Rows Removed by Filter` on that node is small or absent.
- `Execution Time` is lower.

If the planner does not use the new index, do not force it. Read the new plan:
the estimate, the condition text and the index definition will show why.
