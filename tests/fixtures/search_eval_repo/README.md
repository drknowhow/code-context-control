# LedgerLite

LedgerLite is a small invoicing service: an OAuth2-protected HTTP API, a SQLite
ledger, a TypeScript web client and a Go rate-limiting daemon.

- `src/ledgerlite/` Python service (auth, billing, storage, api)
- `web/src/` TypeScript client
- `cmd/ledgerd/` Go daemon with the token-bucket limiter in `internal/ratelimit`
- `docs/` operator documentation

Run the service with `python -m ledgerlite.cli serve`.
