# GridWise LLM

BUP CSE Fest 2026 Hackathon, Online Preliminary.

GridWise LLM is an HTTP API that plans a campus's battery, solar and grid use for the next 24 hours. An LLM reads the operator notes, our code validates what it understood, and a linear program finds the cheapest schedule that follows every rule.

## At a glance

| | |
|---|---|
| Live URL | https://gridwise-llm-v1-0-0.onrender.com |
| Endpoints | `GET /health`, `POST /optimize-energy` |
| Docker image | `smsohel/gridwise-llm:v1.0.2` ([Docker Hub](https://hub.docker.com/r/smsohel/gridwise-llm)) |
| LLM models | Groq `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b` (rotated), then Google `gemini-3.5-flash-lite` as fallback |
| Optimizer | Linear program, HiGHS solver (`scipy`) |
| Source | https://github.com/abdullah09c/bup-hackathon |

## Run it locally

You need Python 3.11+ and a Groq API key (free at console.groq.com).

```bash
git clone https://github.com/abdullah09c/bup-hackathon.git
cd bup-hackathon
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### Set up `.env`

Copy the template, then open `.env` and paste your key:

```bash
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

Only these three lines are required:

```ini
LLM_PROVIDER=groq
LLM_API_KEY=<your-groq-key>
LLM_MODEL=openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none,openai/gpt-oss-20b@low
```

The fallback settings in `.env.example` are optional. `.env` is ignored by git and Docker, so your key stays private.

### Start and test

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
curl http://127.0.0.1:8000/health
python scripts/run_samples.py http://127.0.0.1:8000
```

The first should print `{"status":"ok"}`. The second sends all 10 public sample cases, checks each answer the way the judge does, and should end with `10/10 passed`. Use `curl.exe` instead of `curl` in Windows PowerShell.

To send one sample by hand:

```bash
python -c "import json;d=json.load(open('BUP_CSE_FEST_2026_Participant_Docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json',encoding='utf-8'));json.dump(d['cases'][0]['input'],open('sample.json','w'))"
curl -X POST http://127.0.0.1:8000/optimize-energy -H "Content-Type: application/json" --data @sample.json
```

For sample 1 you should get a `solar_reduction` for hours `[12, 13]` with factor `0.25`, a `no_op` for the second note, 24 hourly rows and `"total_cost_bdt": 38365.0` (the reference optimum).

## Run with Docker

```bash
docker pull smsohel/gridwise-llm:v1.0.2
docker run --rm -p 8000:8000 --env-file .env smsohel/gridwise-llm:v1.0.2
```

The image listens on port 8000, has no keys baked in and runs as a non-root user.

<details>
<summary>Image digest, run without a .env file, build locally</summary>

Digest of `v1.0.2`: `sha256:f1bf5c636fd92f7c4d3cbd6631cb55d4c9635f61608c44c4df717ee473f8aaa3`

Without a `.env` file, pass the variables directly:

```bash
docker run --rm -p 8000:8000 -e LLM_PROVIDER=groq -e LLM_API_KEY=<your-groq-key> -e LLM_MODEL=openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none,openai/gpt-oss-20b@low smsohel/gridwise-llm:v1.0.2
```

The image binds `0.0.0.0`, reads `PORT` if a host sets it, and has a Docker `HEALTHCHECK` on `/health`. To build it yourself: `docker build -t gridwise-llm .`

</details>

## How it works

```
request -> validate input -> LLM reads notes -> guardrails -> optimizer -> replay check -> response
```

- **LLM.** All notes go to the model in one call. It returns the directive type, the start and end hour and the number as written ("80%", "half", "90 kWh"). It never does arithmetic. The prompt is in `app/llm/prompt.py`.
- **Guardrails.** Code turns that into the exact directive: hours with the end excluded, "80% reduction" into factor 0.2, percent of capacity into kWh. It rejects unknown types and out-of-range values. If the model's answer fails, it gets the errors back once, then the next model or fallback is tried. A small rule-based parser is the last resort only if every LLM is down.
- **Optimizer.** A linear program minimizes grid cost under every battery, solar, grid and directive limit, and returns the battery to its starting level at the end of the day.
- **Replay check.** Before sending, the plan is replayed hour by hour against all the rules, the same way the judge does.

<details>
<summary>Guardrails in detail</summary>

- `directive_type` must be one of the six supported types. Anything else is rejected, not mapped to something close.
- Each note gets exactly one interpretation, in `note_index` order. Unknown or duplicate indices are dropped, and a missing note is asked for again.
- Hours come from the window: start included, end excluded, wrapping past midnight when needed. They end up unique, ascending and within 0 to 23.
- Solar factor must be in [0, 1]. A reserve must be at least 0 and no more than battery capacity. A grid cap must be at least 0.
- `no_op` always gets `applies=false` and a `null` adjustment. Every other type gets `applies=true` and the exact shape from the spec.
- A solar reduction that only covers hours with zero solar is almost always an AM/PM slip ("one until three" read as 1 to 3 AM). If the same window 12 hours later has solar, the hours are moved there.
- If validation fails, the errors go back to the model once. Then the next model is tried, then the fallback providers, then the rule parser, and finally `no_op`. The service never invents a directive and doesn't crash on bad model output.

</details>

<details>
<summary>Optimizer and rate limits in detail</summary>

Each hour has five variables: grid, solar used, charge, discharge and battery energy. The constraints are energy balance, battery transitions, `max(base minimum, reserve) <= energy <= capacity`, charge and discharge limits (0 inside no-charge or no-discharge windows), solar used at most forecast × factor, grid at most the cap, and hour 23 ending at the starting battery level.

It minimizes grid × tariff. A tiny penalty on battery throughput stops needless cycling without changing the cost. Charge and discharge are netted into one action per hour, values are rounded to 4 decimals, and totals are recomputed from the rounded plan. On the public pack it matches all 10 reference optimal costs exactly. If SciPy is unavailable, an exact dynamic-programming solver takes over.

Groq's free tier limits tokens per minute for each model separately, so the three primary models are rotated. A model that returns 429 sits out until its `retry-after` time passes, and notes already seen are answered from an in-memory cache.

</details>

## Configuration

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `groq`, `gemini`, `openai`, `openrouter`, or `stub` for local testing without an LLM |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Model, or a comma-separated list to rotate. `@low` / `@none` sets reasoning effort |
| `LLM_FALLBACK_*`, `LLM_FALLBACK2_*` | Optional backup providers (`_PROVIDER`, `_API_KEY`, `_MODEL`), tried in order |
| `PORT` | Listen port, default 8000 |

<details>
<summary>All environment variables</summary>

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | | Preset: `groq`, `gemini`, `openai`, `openrouter`, `commandcode`, `agentrouter`, or `stub` |
| `LLM_API_KEY` | | Key for the primary provider |
| `LLM_MODEL` | preset default | One model or a comma-separated list, rotated. Optional `@effort` per model |
| `LLM_BASE_URL` | preset | OpenAI-compatible base URL, needed only for providers without a preset |
| `LLM_REASONING_EFFORT` | | Reasoning effort for models without an `@` suffix |
| `LLM_JSON_MODE` | `true` | Set `false` for endpoints that reject JSON mode |
| `LLM_FALLBACK_PROVIDER`, `_API_KEY`, `_MODEL`, `_BASE_URL` | | First backup provider |
| `LLM_FALLBACK2_*`, `LLM_FALLBACK3_*` | | Further backups, same variables, tried in order |
| `LLM_FALLBACK_HEADERS` | | Extra HTTP headers for the backup, as a JSON object |
| `LLM_TIMEOUT_SECONDS` | `10` | Timeout per LLM call |
| `LLM_REQUEST_BUDGET_SECONDS` | `20` | Total LLM time per request (judge limit is 30) |
| `PORT` | `8000` | Listen port |
| `WEB_CONCURRENCY` | `2` | Uvicorn workers in Docker |
| `LOG_LEVEL` | `INFO` | Log level |

The deployed service uses a second Groq key as `LLM_FALLBACK_*` and Gemini as `LLM_FALLBACK2_*`. Without `LLM_API_KEY`, the service still starts but uses only the rule-based parser, which is meant for local testing.

</details>

Keys are never logged or returned in responses, and errors never include stack traces.

**API responses:** `200` success, `400` malformed or incomplete request, `422` valid JSON that can't be scheduled, `500` internal error with no details.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                                  # 87 offline tests
python scripts/eval_interpretation.py      # 30 reworded notes through the real LLM
```

<details>
<summary>Project layout</summary>

```
app/main.py            endpoints, error handlers, startup warm-up
app/schemas.py         request and response models
app/llm/prompt.py      prompt and examples
app/llm/client.py      OpenAI-compatible LLM client
app/llm/interpreter.py LLM call, retry, model rotation, fallbacks, cache
app/llm/rule_parser.py rule-based parser (last resort)
app/guardrails.py      directive validation and conversion
app/optimizer.py       linear program and DP fallback
app/validator.py       judge-style replay check
app/pipeline.py        request flow and plan summary
scripts/               sample runner and interpretation eval
tests/                 pytest suite
```

</details>

## Known limitations

- The Groq free tier handles about 15 to 20 new requests per minute. Beyond that, requests go to the fallback providers.
- Interpretation quality depends on the LLM. The rule-based parser only knows common phrasings.
- If a note leaves out AM or PM, it is inferred from context (solar work is daytime, evening peaks are PM).
- A scenario that can't be scheduled returns 422 instead of a partial plan.

## Dependencies

FastAPI, Uvicorn, Pydantic, httpx, NumPy, SciPy (see `requirements.txt`), pytest for tests. LLMs via the Groq API and the Google Gemini API.
