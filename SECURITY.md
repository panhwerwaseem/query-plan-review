# Security

An agent skill is instructions and code that your coding agent will follow and run. Review a skill before installing it, the same way you would review any dependency.

## What this skill does on your machine

| | |
|---|---|
| Reads | The plan file or `pg_stat_statements` CSV file you pass on the command line |
| Writes | Nothing. Output goes to standard output and standard error |
| Executes | Nothing. The scripts start no processes and open no database connection |
| Network | None. The bundled scripts import no network modules. `tools/validate.py` checks this in CI |
| Dependencies | Python standard library only |
| Pre-approved tools | None. `SKILL.md` has no `allowed-tools` entry, so your agent's normal permission prompts apply |
| Hooks, MCP servers, background processes | None |

The scripts only analyse saved files. Capturing those files is done by you or by your agent with your own database client, through your agent's normal permission flow.

Three cautions that are specific to this skill:

- **`EXPLAIN ANALYZE` executes the statement.** On an INSERT, UPDATE, DELETE or MERGE it changes data unless it is wrapped in a transaction that is rolled back, and even then it takes locks, fires triggers and consumes sequence values. On a large SELECT it puts the full load of the query on the server. `SKILL.md` tells the agent not to run it on a write statement against production until you have agreed, and not to run DDL or change settings on a shared database on its own initiative. Give your agent database credentials with that in mind; a read-only role on a non-production copy is the safe default.
- **Plans and statistics contain query text.** A plan shows literal values in its conditions, and `pg_stat_statements` shows statement text and table names. These can include personal or confidential data. Check a file before attaching it to an issue or committing it.
- **The findings are advice.** The scripts report what a plan measured. Whether to create an index or change statistics on your database remains your decision.

## Reporting a vulnerability

Please report security problems privately through the repository's **Security** tab ("Report a vulnerability"). If that option is not available, open an issue that says a private channel is needed, without including exploit details.

## Verifying what you install

- Install from a tagged release and read the diff between tags before updating.
- The whole skill is the `skills/query-plan-review/` folder: one Markdown protocol, two Python scripts, three reference files, and eval fixtures.
