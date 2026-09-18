# Hackathon Operational & Safety Instructions for GitHub Copilot

## 1. General Constraints
- Restrict all code changes, bug fixes, and suggestions strictly to the current repository workspace.
- Do not introduce unrequested external dependencies, rewrite clean code, or alter existing test suites.

## 2. Bug-Fix Hackathon Rules (e.g., IUT CoWork Model)
- **Minimal Surgical Diffs:** Modify only the exact lines causing an issue. Never refactor surrounding working code or reformat unrelated styles.
- **Datetime Invariants:** Use timezone-aware UTC datetimes (`datetime.now(timezone.utc)`). Never use naive datetimes.
- **Financial Math:** Use integer cents for currency calculations (`price_cents = hourly_rate_cents * duration_hours`). Avoid floats.

## 3. AI / SupportOps API Rules (e.g., SUST QueueStorm Model)
- **Hard Safety Failsafes:** Never output prompts requesting PIN, OTP, passwords, CVV, or card details in `customer_reply`. Never make definitive refund promises.
- **Schema Adherence:** Ensure all Pydantic V2 models and JSON responses match required specs precisely.
