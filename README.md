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

## How it works

```
request -> validate input -> LLM reads notes -> guardrails -> optimizer -> replay check -> response
```

- **LLM.** All notes go to the model in one call. It returns the directive type, the start and end hour and the number as written ("80%", "half", "90 kWh"). It never does arithmetic. The prompt is in `app/llm/prompt.py`.
- **Guardrails.** Code turns that into the exact directive: hours with the end excluded, "80% reduction" into factor 0.2, percent of capacity into kWh. It rejects unknown types and out-of-range values. If the model's answer fails, it gets the errors back once, then the next model or fallback is tried. A small rule-based parser is the last resort only if every LLM is down.
- **Optimizer.** A linear program minimizes grid cost under every battery, solar, grid and directive limit, and returns the battery to its starting level at the end of the day.
- **Replay check.** Before sending, the plan is replayed hour by hour against all the rules, the same way the judge does.

## Configuration

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `groq`, `gemini`, `openai`, `openrouter`, or `stub` for local testing without an LLM |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Model, or a comma-separated list to rotate. `@low` / `@none` sets reasoning effort |
| `LLM_FALLBACK_*`, `LLM_FALLBACK2_*` | Optional backup providers (`_PROVIDER`, `_API_KEY`, `_MODEL`), tried in order |
| `PORT` | Listen port, default 8000 |

Keys are never logged or returned in responses, and errors never include stack traces.

**API responses:** `200` success, `400` malformed or incomplete request, `422` valid JSON that can't be scheduled, `500` internal error with no details.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                                  # 87 offline tests
python scripts/eval_interpretation.py      # 30 reworded notes through the real LLM
```

## Known limitations

- The Groq free tier handles about 15 to 20 new requests per minute. Beyond that, requests go to the fallback providers.
- Interpretation quality depends on the LLM. The rule-based parser only knows common phrasings.
- If a note leaves out AM or PM, it is inferred from context (solar work is daytime, evening peaks are PM).
- A scenario that can't be scheduled returns 422 instead of a partial plan.

## Dependencies

FastAPI, Uvicorn, Pydantic, httpx, NumPy, SciPy (see `requirements.txt`), pytest for tests. LLMs via the Groq API and the Google Gemini API.
