# Bug-Fix Competition Rules (e.g., IUT CoWork Style)

## Minimal Surgical Diffs
- Isolate failures using error logs and target test cases.
- Apply the minimum necessary change to fix the root cause. Do not reformat surrounding code or alter unrelated variables.

## Data & Datetime Handling
- **Datetimes:** Always use timezone-aware UTC datetime objects (`datetime.now(timezone.utc)`). Never use naive datetimes (`datetime.now()`).
- **Financial Math:** All pricing calculations must use integer cents: `price_cents = hourly_rate_cents * duration_hours`. Avoid floats to prevent rounding errors.
- **Concurrency & SQLite:** Ensure database sessions are explicitly closed and transactions are concise to prevent table locks or deadlocks under concurrent execution.

## Documentation Requirements
- Maintain `bug_report.md` contemporaneously. Document file paths, exact line numbers, root cause analysis, and the applied fix for every resolved issue.
