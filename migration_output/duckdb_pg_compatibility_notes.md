# DuckDB to PostgreSQL Compatibility Notes

This migration uses conservative PostgreSQL mappings and prefers TEXT where exact fidelity is uncertain.

## Observed DuckDB Types
- `BIGINT` -> `BIGINT`
- `BOOLEAN` -> `BOOLEAN`
- `DATE` -> `DATE`
- `DOUBLE` -> `DOUBLE PRECISION`
- `FLOAT` -> `REAL`
- `HUGEINT` -> `BIGINT`
- `INTEGER` -> `INTEGER`
- `JSON` -> `JSONB`
- `TIMESTAMP` -> `TIMESTAMP`
- `TIMESTAMP WITH TIME ZONE` -> `TIMESTAMPTZ`
- `VARCHAR` -> `TEXT`

## Potential Concerns
- JSON columns are mapped to JSONB only when the DuckDB type is explicitly JSON; JSON-like VARCHAR/TEXT content will remain TEXT and be copied verbatim.
- TIMESTAMP WITH TIME ZONE is mapped to TIMESTAMPTZ. Plain TIMESTAMP stays TIMESTAMP without timezone.
- DATE stays DATE.
- BLOB or binary-like types are mapped to BYTEA. If the source stores non-bytes Python objects, row-level conversion may still be required.
- Array-like or nested DuckDB types are downgraded to TEXT for safety in this first migration pass.
- Views are inventoried but not migrated into PostgreSQL in this pass.
- Indexes are deferred until after data load. This reduces load-time overhead and avoids committing to DuckDB-specific index metadata too early.
- Insert or replace semantics are not replayed. The migration copies final table contents as-is into PostgreSQL.
- Constraints and foreign keys are intentionally minimal in the generated schema to avoid destructive assumptions during the first import.
