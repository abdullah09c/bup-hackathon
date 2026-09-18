# GridWise LLM — Smart Campus Energy Optimizer

BUP CSE Fest 2026 Hackathon · Online Preliminary · LLM-assisted operator directive interpretation.

One HTTP API that reads a 24-hour campus energy scenario plus 1–3 natural-language operator notes,
**interprets the notes with an LLM**, validates the interpretation with deterministic guardrails,
applies it to a **linear-programming optimizer**, re-verifies the finished schedule, and returns the
minimum-cost valid plan.

| Item | Value |
|---|---|
| Endpoints | `GET /health`, `POST /optimize-energy` |
| Port | `8000` (override with `PORT`), binds `0.0.0.0` |
| LLM | Groq `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b` (round-robin) → Groq fallback key → Google `gemini-3.5-flash-lite` → rule parser |
| Optimizer | Linear program, HiGHS solver via `scipy.optimize.linprog` (exact DP fallback) |
| Live URL | `<PUBLIC_BASE_URL>` |
| Docker image | `smsohel/gridwise-llm:v1.0.1` (digest `sha256:dfc3177a9ac1fc85e84006c56ef32af93a710ce3d04bb38a7069c723b2c727b3`) |

---

## 1. Architecture

```
request ──► schema validation (400 on bad structure)
        ──► LLM interpreter ──► guardrails ──► optimizer (HiGHS LP) ──► replay validator ──► response
                 │   ▲              │
                 │   └─ retry once with validation errors as feedback
                 ├─► fallback tiers in order (LLM_FALLBACK_*, LLM_FALLBACK2_*, …)
                 └─► rule-based parser → no_op   (last resort only; never invents a directive)
```

**LLM role.** The LLM is the interpreter of `operator_notes`. For every note it returns *raw* fields:
directive type, start/end clock hour, a value and the kind of value
(`percent_reduction`, `percent_remaining`, `fraction_remaining`, `kwh`, `percent_of_capacity`, …).
All notes of a request go in **one** call (temperature 0, JSON mode). Prompt: `app/llm/prompt.py`.

**Guardrails** (`app/guardrails.py`) — LLM output is untrusted until it passes:
- `directive_type` must be one of the six supported types; anything else is rejected.
- exactly one interpretation per note, in `note_index` order; unknown/duplicate indices ignored, missing ones re-requested.
- hours are built by code from the window: start inclusive, end exclusive (`1 PM–3 PM → [13,14]`), midnight wrap handled, unique, ascending, 0–23.
- all arithmetic is done in code, not by the LLM: `80% reduction → factor 0.2`, `50% of capacity → kWh`.
- solar factor in [0,1]; reserve finite, ≥ 0 and ≤ battery capacity; grid cap finite and ≥ 0.
- `no_op` ⇒ `applies=false`, `structured_adjustment=null`; every other type ⇒ `applies=true` with the exact shape.
- failures are fed back to the LLM once; then the next model / fallback endpoint; then the rule-based parser; then `no_op`.
- scenario-aware AM/PM check: a solar reduction that only covers zero-solar hours is shifted 12 h when that lands on daylight hours.
- rate limits: a model that returns 429 is skipped for its `retry-after` period; models rotate round-robin.

**Optimizer** (`app/optimizer.py`) — per hour: `grid, solar_used, charge, discharge, E`.
Constraints: energy balance, battery transition, `max(base_min, reserve) ≤ E ≤ capacity`, rate limits
(0 inside no-charge / no-discharge windows), `solar_used ≤ solar × factor`, `grid ≤ cap`, `E[23] = initial`.
Objective: minimize `Σ grid × tariff` (plus a 1e-6 penalty on battery throughput to avoid pointless cycling).
Charge and discharge are netted into one action per hour; values are rounded to 4 decimals and
totals are recomputed from the rounded plan.

**Final replay** (`app/validator.py`) replays every plan hour by hour exactly like the judge
(transitions, bounds, rate limits, directive windows, balance, neutrality, totals) before it is returned.

**Verification on the public pack:** the LP reproduces all 10 reference optimal costs exactly, and every
response passes the replay against the ground-truth directives.

---

## 2. Quick start (local)

Requires Python 3.11+.

```bash
git clone <REPO_URL> gridwise && cd gridwise
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then put your key in .env (LLM_API_KEY=...)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The service reads `.env` from the working directory (real environment variables take precedence).
Without `LLM_API_KEY` it starts in **stub mode** (rule-based interpreter) and logs a warning — for local
testing only; the submitted deployment always runs with an LLM configured.

### Health check
```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

### Optimize a public sample
```bash
python -c "import json;d=json.load(open('BUP_CSE_FEST_2026_Participant_Docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json',encoding='utf-8'));json.dump(d['cases'][0]['input'],open('sample.json','w'))"
curl -X POST http://127.0.0.1:8000/optimize-energy -H "Content-Type: application/json" --data @sample.json
```

Response (abridged):
```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [12, 13], "factor": 0.25}, "explanation": "..."},
    {"note_index": 1, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null, "explanation": "..."}
  ],
  "hourly_plan": [{"hour": 0, "grid_kwh": 90.0, "solar_used_kwh": 0.0, "battery_action": "idle",
                   "battery_kwh": 0.0, "battery_energy_after_kwh": 110.0}, "... 23 more"],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "..."
}
```

### Run all public samples (judge-style check)
```bash
python scripts/run_samples.py http://127.0.0.1:8000            # or your public URL
python scripts/run_samples.py https://<PUBLIC_BASE_URL> --repeat 3
```
For each case it checks HTTP 200, the interpretation against the reference, replays `hourly_plan`
against the **true** directives, compares cost with the reference optimum, and prints p50/p95 latency.
Expected result: `10/10 passed`.

### Interpretation accuracy (real LLM)
```bash
python scripts/eval_interpretation.py                 # 30 paraphrased notes through the configured models
python scripts/eval_interpretation.py openai/gpt-oss-120b@low --pace 8   # one model
```
Latest run: 30/30 for the rotation, gpt-oss-120b and qwen3.8-27b; p95 ≈ 2–3.5 s per request.

### Unit tests
```bash
pip install -r requirements-dev.txt
pytest -q
```
Covers the optimizer (all 10 reference costs), guardrails, interpretation chain with a mocked LLM
(success, retry-with-feedback, provider failure → fallback, all providers down), and the HTTP contract (400/422).

---

## 3. Docker (fallback execution path)

```bash
docker pull smsohel/gridwise-llm:v1.0.1
docker run --rm -p 8000:8000 -e LLM_PROVIDER=groq -e LLM_API_KEY=<your-key> -e LLM_MODEL=openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none,openai/gpt-oss-20b@low smsohel/gridwise-llm:v1.0.1
curl http://127.0.0.1:8000/health
```
Or with an env file: `docker run --rm -p 8000:8000 --env-file .env smsohel/gridwise-llm:v1.0.1`.

Build locally: `docker build -t gridwise-llm .` — the image contains no secrets, runs as a non-root user,
exposes port 8000 and binds `0.0.0.0` (`PORT` overridable).

---

## 4. Configuration (environment variable names)

| Variable | Required | Meaning |
|---|---|---|
| `LLM_PROVIDER` | yes | Preset: `gemini`, `commandcode`, `openai`, `groq`, `openrouter`, `agentrouter`, or `stub` (no LLM, local testing only) |
| `LLM_API_KEY` | yes | API key for the primary LLM |
| `LLM_MODEL` | no | Model id, or a comma-separated list rotated round-robin (each model has its own provider rate limit). Optional `@effort` suffix per model sets `reasoning_effort`, e.g. `openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none,openai/gpt-oss-20b@low` |
| `LLM_BASE_URL` | no | OpenAI-compatible base URL (required for providers without a preset) |
| `LLM_REASONING_EFFORT` | no | Sent as `reasoning_effort` (e.g. `none`/`low`) to cut latency on reasoning models |
| `LLM_JSON_MODE` | no | `false` disables `response_format: json_object` for endpoints that reject it |
| `LLM_FALLBACK_PROVIDER` / `_API_KEY` / `_MODEL` / `_BASE_URL` | no | First fallback tier (any OpenAI-compatible provider), tried when every primary model fails |
| `LLM_FALLBACK2_*`, `LLM_FALLBACK3_*` | no | Further fallback tiers, same variables, tried in order |
| `LLM_FALLBACK_HEADERS` | no | Extra HTTP headers for the fallback, JSON object |
| `LLM_TIMEOUT_SECONDS` | no | Per LLM call (default 10) |
| `LLM_REQUEST_BUDGET_SECONDS` | no | Total LLM time per request (default 20; the judge limit is 30) |
| `PORT` | no | Listen port (default 8000) |
| `WEB_CONCURRENCY` | no | Uvicorn workers in Docker (default 2) |
| `LOG_LEVEL` | no | Default `INFO` |

**Secret handling:** keys are read only from the environment / an untracked `.env` (git- and docker-ignored).
They are never logged or returned; LLM errors are reduced to a status label; the 500 handler returns
`{"error":"internal_error"}` without stack traces.

---

## 5. API behavior

| Code | When |
|---|---|
| 200 | health OK / successful optimization |
| 400 | malformed JSON or structurally invalid request (missing fields, ≠24 unique hours, 0 or >3 notes, blank note, negative/NaN numbers, wrong types) |
| 422 | well-formed but unschedulable (e.g. initial energy outside [minimum, capacity], or no feasible plan) |
| 500 | controlled internal error, no details exposed |

---

## 6. Project layout

```
app/main.py            FastAPI app, endpoints, error handlers
app/schemas.py         Pydantic request/response models
app/llm/prompt.py      system prompt + few-shot examples (paraphrased, not the public notes)
app/llm/client.py      OpenAI-compatible chat client, JSON extraction
app/llm/interpreter.py LLM → guardrails, retry with feedback, fallback chain, cache
app/llm/rule_parser.py rule-based parser (stub / last resort only)
app/guardrails.py      deterministic validation + normalization of directives
app/optimizer.py       LP (HiGHS) + DP fallback, plan assembly
app/validator.py       judge-style replay
app/pipeline.py        orchestration + plan_summary
scripts/run_samples.py judge-style check against any running URL
tests/                 pytest suite
```

---

## 7. Known limitations

- Interpretation accuracy depends on the configured LLM; the rule-based parser only covers common phrasings and is used solely when the LLM is unavailable.
- Each note maps to exactly one directive (per the Problem Statement); a note that combines two constraints keeps the one the LLM judges primary.
- Ambiguous times without AM/PM are resolved from context (solar work → daytime; evening peaks → PM).
- An unschedulable scenario returns 422 rather than a partially valid plan.
- Hosted LLM availability, quota and rate limits are external dependencies; the fallback endpoint and rule parser reduce but do not remove this risk.

---

## 8. Dependencies & credits

FastAPI, Uvicorn, Pydantic v2, httpx, NumPy, SciPy (HiGHS solver) — see `requirements.txt`; pytest for tests.
LLM providers: Groq API — `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`; Google Gemini API (OpenAI-compatible endpoint) — `gemini-3.5-flash-lite` as fallback.
AI coding assistants were used during development, as permitted by the rulebook.
