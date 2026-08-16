-- Mark the first (bootstrap) configurator as immutable.

-- This migration is intentionally empty: the application now runs an explicit
-- Python migration (see scripts/migrate_bootstrap.py) to add the is_bootstrap
-- column idempotently and backfill existing data. SQLite's executescript does
-- not support conditional ALTER TABLE, so the conditional logic lives in code.
