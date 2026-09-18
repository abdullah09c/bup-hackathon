# AI SupportOps & API Engineering Rules (e.g., SUST QueueStorm Style)

## Hard Safety Failsafes (Zero Tolerance for Penalties)
- **Credential Safety:** NEVER request customer PIN, OTP, passwords, CVV, or full card details in `customer_reply` or logs (-15 pts penalty).
- **Financial Safety:** NEVER issue definitive, binding promises of refunds or account reversals without explicit system confirmation (-15 pts penalty).

## Schema Adherence & Performance
- Enforce strict Pydantic V2 data validation on all incoming payloads and outgoing JSON responses.
- Ensure all API routes conform precisely to specified endpoints, headers, and HTTP status codes.
- Keep response times under 30 seconds for processing pipelines and ensure `GET /health` boots within 60 seconds.
