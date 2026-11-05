# Changelog

All notable changes to this skill are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## 0.1.0 - 2026-11-06

### Added

- `SKILL.md` with the review protocol: six hard rules and an eight-step workflow.
- `scripts/plan_review.py` for PostgreSQL JSON plans: filter-discard scans, repeated inner sides and subplans, disk spills, row misestimates, estimate-only plans, and exclusive time per node.
- `scripts/slow_queries.py` for `pg_stat_statements` CSV exports: ranking by total time and N+1 candidates.
- References for reading plans, index advice, and tracing SQL back to ORM code.
- Six plan fixtures, two statistics fixtures and a small Django app, with eval cases and trigger queries.
- Unit tests, a format validator, and a CI workflow.
