# Deployment

LedgerLite ships as a single container. The reference layout is
`configs/docker-compose.yml`: the Python API, the Go daemon `ledgerd`, and a
volume for the SQLite database.

## Migrations

Run `python -m ledgerlite.cli migrate` before starting a new version. The
migration runner is idempotent; `migrate_v2` adds the `tax_rate` column.

## Health

`GET /healthz` returns 200 once the ledger is open and the limiter is primed.
