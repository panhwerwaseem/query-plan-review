# Tracing SQL back to ORM code

How to go from a statement in a plan or in `pg_stat_statements` to the line of
application code that issues it, and the usual fix for an N+1 in each ORM.
Option and setting names change between ORM versions. If one named here is
rejected, check the ORM's documentation for the installed version.

## Contents

- [From a statement to the code](#from-a-statement-to-the-code)
- [Confirming an N+1](#confirming-an-n1)
- [Django](#django)
- [SQLAlchemy](#sqlalchemy)
- [ActiveRecord (Rails)](#activerecord-rails)
- [Prisma](#prisma)
- [Hibernate and JPA](#hibernate-and-jpa)

## From a statement to the code

`pg_stat_statements` stores each statement with its constants replaced by
`$1`, `$2` and so on. What is left identifies the model and the access path.

1. **Table to model.** Take the table after `FROM`. `scripts/slow_queries.py`
   prints it for each N+1 candidate. Find the model mapped to it; default
   naming is in the table below.
2. **Lookup column to relation.** The column compared with a parameter is the
   key. A foreign-key column (`order_id`) points to a relation access
   (`order.items`). A primary key (`id`) points to a to-one access
   (`item.product`) or a get-by-id call.
3. **Search the code** for that relation or filter: the attribute name, the
   model's query calls, and serializers or templates that walk the relation.
   Templates and serializers are where per-row access usually hides.
4. **Confirm** by logging the SQL for one request (per-ORM sections below) and
   matching the logged statement to the one you started from.

| ORM | Default table name | What its SQL looks like |
|---|---|---|
| Django | `<app label>_<model name in lower case>` | Every identifier quoted and qualified: `"shop_order"."customer_id"` |
| SQLAlchemy | Whatever `__tablename__` says | Unquoted, qualified names: `orders.customer_id` |
| ActiveRecord | Plural snake_case of the model name | `SELECT "orders".* FROM "orders" WHERE "orders"."customer_id" = $1` |
| Prisma | The model name as written, unless `@@map` renames it | Schema-qualified and quoted: `"public"."Order"."customerId"` |
| Hibernate | The entity name, changed by the configured naming strategy | Generated aliases such as `o1_0.customer_id` |

If the application adds SQL comments with the request or code location (for
example Rails query log tags, or the sqlcommenter libraries), the comment in
the logged statement names the controller or view directly.

## Confirming an N+1

An N+1 is one query that returns N rows followed by one more query per row.
In `pg_stat_statements` it shows as a pair:

- a parent statement with `calls` = C and `rows` / `calls` = k, and
- a child statement, a lookup by one key, with `calls` close to C times k.

That ratio is supporting evidence. The proof is in the code: a loop over the
parent's result with a relation access or query inside it. A statement with
the same shape that is issued once per request (a session or user lookup) is
not an N+1.

The fix is always the same idea: load the related rows for all parents in one
query (a join, or a second query with `IN`), before the loop. Verify by
counting the queries the code path issues before and after. The count must
stop growing with the number of rows.

An index does not fix an N+1. Each child lookup is usually already fast; the
cost is the number of round trips.

## Django

**See the SQL.** `str(queryset.query)` gives an approximation (parameters are
not quoted). `django.db.connection.queries` lists executed statements, only
when `DEBUG = True`. `django.test.utils.CaptureQueriesContext` captures them
in tests regardless of `DEBUG`.

**Get the plan.** `queryset.explain(analyze=True, buffers=True, format="json")`
returns the plan as a string. With `analyze=True` it executes the query.

**N+1 fix.**

| Access in the loop | Fix on the queryset |
|---|---|
| Forward foreign key or one-to-one (`item.product`) | `select_related("product")`: one query with a join |
| Reverse foreign key or many-to-many (`order.items.all()`) | `prefetch_related("items")`: one extra query with `IN` |
| A filtered or ordered related set | `prefetch_related(Prefetch("items", queryset=...))` |
| A count per row (`order.items.count()`) | `annotate(n=Count("items"))` on the parent queryset |

Calling `.filter()` or `.order_by()` on a prefetched relation inside the loop
builds a different query, so it runs again per row and bypasses the prefetch.

**Guard.** `self.assertNumQueries(n)` in a `TestCase` fails when the query
count changes.

**Index.** `db_index=True` on the field, or `models.Index(fields=[...],
name=...)` in `Meta.indexes`, plus a migration. `ForeignKey` columns are
indexed by default. For a large table see the concurrent build in
`index-advice.md`.

## SQLAlchemy

**See the SQL.** `create_engine(url, echo=True)`, or set the
`sqlalchemy.engine` logger to `INFO`. To print one statement with its values:
`stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})`.

**Get the plan.** Execute `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` followed by
the compiled statement through `session.execute(text(...))`, or copy the logged
SQL into psql.

**N+1 fix.** Relationships load lazily by default: each first access issues a
query. Add a loader option to the parent query:

| Relation | Option |
|---|---|
| Collection (one-to-many, many-to-many) | `select(Order).options(selectinload(Order.items))`: one extra query with `IN` |
| Many-to-one | `joinedload(Item.product)`: a join in the same query |

**Guard.** `raiseload("*")` as a query option, or `lazy="raise"` on the
relationship, turns an unplanned lazy load into an error.

**Index.** `index=True` on the column, or `Index("name", Model.a, Model.b)` in
the table definition, plus an Alembic migration.

## ActiveRecord (Rails)

**See the SQL.** `relation.to_sql`. The development log shows each statement;
with `config.active_record.verbose_query_logs = true` it also shows the line
of application code that triggered it. Rails 7.0 and later can append a
comment naming the controller and action to each statement with
`config.active_record.query_log_tags_enabled = true`.

**Get the plan.** `relation.explain` prints the plan without ANALYZE. Recent
Rails versions accept options such as `relation.explain(:analyze)`; if yours
does not, run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on `relation.to_sql`
in psql.

**N+1 fix.**

| Method | Behaviour |
|---|---|
| `includes(:items)` | Loads the association up front; Rails chooses separate queries or a join |
| `preload(:items)` | Always separate queries with `IN` |
| `eager_load(:items)` | Always one query with a LEFT OUTER JOIN |

For a count per row, use a counter cache or a grouped count query instead of
`order.items.count` in the loop.

**Guard.** `strict_loading` on a relation or a model (Rails 6.1 and later)
raises on lazy loading. The Bullet gem reports N+1 queries in development.

**Index.** `add_index :orders, [:customer_id, :created_at]` in a migration.
For a large table see the concurrent build in `index-advice.md`.

## Prisma

**See the SQL.** `new PrismaClient({ log: ["query"] })` prints each statement.
With `log: [{ emit: "event", level: "query" }]` and `prisma.$on("query", ...)`
the event carries `query`, `params` and `duration`.

**Get the plan.** Copy the logged statement and its parameters into psql.

**N+1 fix.** The pattern is a loop that calls `findMany` or `findUnique` for
each parent row.

- Load the relation with the parent: `include: { items: true }` or a nested
  `select`. Prisma then loads the relation for all parents together.
- Or collect the keys and issue one query: `where: { orderId: { in: ids } }`.
- In GraphQL resolvers, `findUnique` calls made in the same tick are batched
  into one query, so resolving a relation through `findUnique` and its fluent
  relation call avoids the per-row query.

Some Prisma versions can load relations with a database join
(`relationLoadStrategy`). Whether it is available and stable depends on the
version; check the Prisma documentation.

**Index.** `@@index([customerId, createdAt])` on the model in `schema.prisma`,
then generate a migration.

## Hibernate and JPA

**See the SQL.** Set the `org.hibernate.SQL` logger to `DEBUG`, or
`hibernate.show_sql=true` to print to standard output. Bound values are logged
by `org.hibernate.orm.jdbc.bind` at `TRACE` in Hibernate 6 and by
`org.hibernate.type.descriptor.sql.BasicBinder` at `TRACE` in Hibernate 5.
`hibernate.generate_statistics=true` logs how many statements each session
executed. In Spring Boot these go under `spring.jpa.properties.`.

**Get the plan.** Copy the logged statement and its bound values into psql.

**N+1 fix.** Two sources: a lazy association touched in a loop, and a to-one
association left at its JPA default of `FetchType.EAGER`, which Hibernate
can load with one extra select per row when the parent comes from a JPQL query.

| Fix | Use |
|---|---|
| `JOIN FETCH` in JPQL: `select o from Order o join fetch o.items where ...` | One query with a join |
| `@EntityGraph(attributePaths = {"items"})` on a Spring Data repository method | The same, declared on the method |
| `@BatchSize(size = n)` on the association, or `hibernate.default_batch_fetch_size` | Loads the association for several parents at once with `IN` |
| A DTO projection query | Selects only the needed columns in one statement |

Do not combine `JOIN FETCH` on a collection with `setMaxResults` pagination.
Hibernate then fetches every row and paginates in memory, and logs a warning
about it. Use batch fetching, or page the parent IDs first.

**Index.** `@Index` inside `@Table(indexes = ...)` affects only schema
generation. In a project that manages its schema with Flyway or Liquibase, add
the index in a migration.
