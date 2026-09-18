# GridWise LLM: Smart Campus Energy Optimizer

BUP CSE Fest 2026 Hackathon, Online Preliminary. LLM-assisted operator directive interpretation.

GridWise LLM is a single HTTP API. You send it a 24-hour campus energy scenario (demand, solar, tariff and battery limits) along with 1 to 3 operator notes written in plain English. An LLM reads the notes and turns each one into a structured directive. Our code checks that directive, a linear program finds the cheapest schedule that respects it, and we replay the finished schedule against every rule before sending it back.

| Item | Value |
|---|---|
| Live URL | https://gridwise-llm-v1-0-0.onrender.com |
| Endpoints | `GET /health`, `POST /optimize-energy` |
| Docker image | `smsohel/gridwise-llm:v1.0.2` (digest `sha256:f1bf5c636fd92f7c4d3cbd6631cb55d4c9635f61608c44c4df717ee473f8aaa3`) |
| Port | `8000`, binds `0.0.0.0` (the `PORT` variable overrides it) |
| LLM | Groq: `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b` in rotation. Fallbacks: a second Groq key, then Google `gemini-3.5-flash-lite` |
| Optimizer | Linear program solved with HiGHS (`scipy.optimize.linprog`), with an exact dynamic-programming fallback |
| Source | https://github.com/abdullah09c/bup-hackathon |

---

## 1. Quick start (local)

You need Python 3.11 or newer and one Groq API key (free at console.groq.com).

### Step 1: get the code and install

```bash
git clone https://github.com/abdullah09c/bup-hackathon.git
cd bup-hackathon
python -m venv .venv
```

Activate the environment:

- Windows (cmd or PowerShell): `.venv\Scripts\activate`
- macOS / Linux: `source .venv/bin/activate`

```bash
pip install -r requirements.txt
```

### Step 2: configure the LLM

Copy the template (`copy .env.example .env` on Windows, `cp .env.example .env` elsewhere) and fill in the key. These three lines are all you need:

```ini
LLM_PROVIDER=groq
LLM_API_KEY=<your-groq-key>
LLM_MODEL=openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none,openai/gpt-oss-20b@low
```

You can skip the fallback tiers in `.env.example`. The service reads `.env` from the folder you start it in, and a real environment variable wins over the file if both are set. Git and Docker both ignore `.env`, so the key stays out of the repo and the image.

### Step 3: start the service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`/health` answers a few seconds after start. We load the solver before opening the port so the first real request doesn't pay for it.

### Step 4: check health

```bash
curl http://127.0.0.1:8000/health
```

You should get `{"status":"ok"}` back.

In Windows PowerShell, type `curl.exe` instead of `curl`. PowerShell maps plain `curl` to a different command.

### Step 5: send a public sample

The first line writes public sample 1 to `sample.json`; the second posts it:

```bash
python -c "import json;d=json.load(open('BUP_CSE_FEST_2026_Participant_Docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json',encoding='utf-8'));json.dump(d['cases'][0]['input'],open('sample.json','w'))"
curl -X POST http://127.0.0.1:8000/optimize-energy -H "Content-Type: application/json" --data @sample.json
```

The response looks like this (shortened):

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
     "explanation": "Solar at 25% during cleaning."},
    {"note_index": 1, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null,
     "explanation": "Unrelated campus news."}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 90.0, "solar_used_kwh": 0.0, "battery_action": "idle",
     "battery_kwh": 0.0, "battery_energy_after_kwh": 110.0}
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Applied solar_reduction; ignored 1 unrelated note(s). Charges 335 kWh in hours 2-4, 13-15, 22-23 and discharges 335 kWh in hours 1, 10-12, 17-20, ..."
}
```

The real `hourly_plan` has 24 entries; we show one. The LLM writes the explanation text, so the wording can change from run to run. The directives, hours, factor and the 38365 BDT cost should match exactly. That cost is the reference optimum for this case.

To try the live service instead, swap `http://127.0.0.1:8000` for `https://gridwise-llm-v1-0-0.onrender.com`.

---

## 2. Testing against the public samples

### Judge-style check of all 10 public cases

```bash
python scripts/run_samples.py http://127.0.0.1:8000
python scripts/run_samples.py https://gridwise-llm-v1-0-0.onrender.com --repeat 2
```

For each case, the script does four things:

1. Checks for HTTP 200.
2. Compares the interpretation with the reference: type, hours and values, within 0.01.
3. Replays `hourly_plan` hour by hour against the reference directives, the way the judge does.
4. Compares the cost with the reference optimum.

At the end it prints p50 and p95 latency. A healthy run ends with `10/10 passed`; if any case fails, the script exits with code 1.

### Interpretation accuracy on paraphrased notes

```bash
python scripts/eval_interpretation.py
```

We wrote 30 reworded notes to see how the models cope with wording they haven't seen: percentages, fractions, "noon" and "midnight", windows that cross midnight, reserves given as a percent of capacity, and distractors that mention solar or tariffs without changing anything. Our last run scored 30/30, with p95 between 2 and 3.5 seconds per request. If you hit free-tier rate limits, add `--pace 8`. To test a single model, pass its name, for example `openai/gpt-oss-120b@low`.

### Unit tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite has 87 tests, and none of them call the network. They cover:

- the optimizer against all 10 reference costs
- the guardrails
- the interpretation chain with a mocked LLM: a retry with feedback, a rate-limited primary, fallback tiers in order, and every provider down
- the HTTP contract: 400 and 422 cases, field order and the `scenario_id` echo

---

## 3. Docker fallback

```bash
docker pull smsohel/gridwise-llm:v1.0.2
docker run --rm -p 8000:8000 \
  -e LLM_PROVIDER=groq \
  -e LLM_API_KEY=<your-groq-key> \
  -e LLM_MODEL=openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none,openai/gpt-oss-20b@low \
  smsohel/gridwise-llm:v1.0.2
curl http://127.0.0.1:8000/health
```

If you already have a filled-in `.env`, this is shorter: `docker run --rm -p 8000:8000 --env-file .env smsohel/gridwise-llm:v1.0.2`.

The image has no keys or `.env` inside and runs as a non-root user. It exposes port 8000 and binds `0.0.0.0`; hosting platforms can change the port through `PORT`. Its Docker `HEALTHCHECK` calls `/health`. To build it yourself, run `docker build -t gridwise-llm .`

---

## 4. How it works

```
request
  -> schema validation                  (400 on malformed or incomplete input)
  -> LLM interpreter                    (one call for all notes, JSON output)
       retry once with the validation errors as feedback
       next model in rotation, then the fallback tiers in order
       rule-based parser only if every LLM fails
  -> guardrails                         (deterministic checks and all arithmetic)
  -> optimizer                          (HiGHS linear program)
  -> replay validator                   (same checks as the judge)
  -> response
```

### What the LLM does

The LLM interprets `operator_notes`. We send all the notes of a request in one call (temperature 0, JSON mode). For each note the model gives back the directive type, the start and end clock hour, the number exactly as written, and what kind of number it is (`percent_reduction`, `percent_remaining`, `fraction_remaining`, `kwh`, `percent_of_capacity` and a few more). We never ask it to convert anything. Our code turns "80% reduction" into factor 0.2, "1 PM to 3 PM" into hours `[13, 14]` and "half the capacity" into kWh, because those conversions are where a model is most likely to slip. The prompt lives in `app/llm/prompt.py`; we reworded its examples rather than copying the public cases.

The rule-based parser in `app/llm/rule_parser.py` is not the interpreter. It only runs when every configured LLM has failed on a request, so the service can still answer safely instead of returning an error, and its output goes through the same guardrails.

### Guardrails

We treat the model's output as untrusted until `app/guardrails.py` has checked all of the following:

- `directive_type` has to be one of the six supported types. We reject anything else instead of mapping it to something close.
- Each note gets exactly one interpretation, in `note_index` order. We drop unknown and duplicate indices and ask again for any note that's missing.
- Our code builds the hours from the window: start included, end excluded, wrapping past midnight when needed. The result is unique, ascending and within 0 to 23.
- Solar factor must be in [0, 1]. A reserve must be finite, at least 0 and no more than capacity. A grid cap must be finite and at least 0.
- `no_op` always gets `applies=false` and a `null` adjustment. Every other type gets `applies=true` and exactly the adjustment shape the spec defines.
- A solar reduction that lands only on hours with zero forecast solar is almost always an AM/PM slip ("one until three" read as 1 to 3 AM). If the same window 12 hours later has solar, we move the hours there.
- When validation fails, we send the errors back to the LLM once. If that doesn't fix it, we try the next model, then the fallback tiers, then the rule parser, and finally `no_op`. Bad model output can't make the service invent a directive or crash.

### Optimizer

`app/optimizer.py` has five variables per hour: grid, solar used, charge, discharge and battery energy. The constraints are:

- energy balance
- battery transitions
- `max(base minimum, active reserve) <= energy <= capacity`
- charge and discharge rate limits, forced to 0 inside no-charge and no-discharge windows
- solar used at most forecast × factor
- grid at most the cap
- battery energy at hour 23 equal to the starting value

It minimizes the sum of grid × tariff. A tiny 1e-6 penalty on battery throughput stops it from cycling the battery for no reason, and it doesn't change the cost. We net each hour's charge and discharge into a single action, round values to 4 decimals, and recompute the totals from the rounded plan so they agree with what the judge calculates.

### Final replay

Before a plan goes out, `app/validator.py` replays it hour by hour and checks transitions, bounds, rate limits, directive windows, energy balance, the end-of-day battery level and the totals. On the public pack, the linear program hits all 10 reference optimal costs exactly.

### Rate limits

Groq's free tier caps tokens per minute for each model separately. We rotate through the three primary models so the load spreads over three budgets. A model that answers 429 sits out until its `retry-after` time passes, and notes we've already seen come straight from an in-memory cache.

---

## 5. Configuration

This table lists names only. Put the values in `.env` or in your hosting platform's environment settings.

| Variable | Required | Meaning |
|---|---|---|
| `LLM_PROVIDER` | yes | Preset: `groq`, `gemini`, `openai`, `openrouter`, `commandcode`, `agentrouter`, or `stub` (no LLM, local testing only) |
| `LLM_API_KEY` | yes | Key for the primary provider |
| `LLM_MODEL` | no | One model or a comma-separated list used in rotation. An optional `@effort` suffix per model sets `reasoning_effort`, e.g. `openai/gpt-oss-120b@low` |
| `LLM_BASE_URL` | no | OpenAI-compatible base URL, needed only for providers without a preset |
| `LLM_REASONING_EFFORT` | no | Default `reasoning_effort` for models without an `@` suffix |
| `LLM_JSON_MODE` | no | `false` turns off `response_format: json_object` for endpoints that reject it |
| `LLM_FALLBACK_PROVIDER`, `_API_KEY`, `_MODEL`, `_BASE_URL` | no | First fallback tier, tried when every primary model fails |
| `LLM_FALLBACK2_*`, `LLM_FALLBACK3_*` | no | Further fallback tiers with the same variables, tried in order |
| `LLM_FALLBACK_HEADERS` | no | Extra HTTP headers for the fallback, as a JSON object |
| `LLM_TIMEOUT_SECONDS` | no | Timeout per LLM call, default 10 |
| `LLM_REQUEST_BUDGET_SECONDS` | no | Total LLM time per request, default 20 (the judge limit is 30) |
| `PORT` | no | Listen port, default 8000 |
| `WEB_CONCURRENCY` | no | Uvicorn workers in Docker, default 2 |
| `LOG_LEVEL` | no | Default `INFO` |

The deployed service uses a second Groq key as `LLM_FALLBACK_*` (`openai/gpt-oss-120b@low,qwen/qwen3.8-27b@none`) and Gemini as `LLM_FALLBACK2_*` (`gemini-3.5-flash-lite`).

Without `LLM_API_KEY` the service still starts, logs a warning and falls back to the rule-based parser. That mode is for local testing only. Our deployed service always runs with an LLM configured.

### Secrets

The service reads keys only from the environment or the untracked `.env`. It never logs them or puts them in a response. When an LLM call fails, the log shows a status label such as `LLM HTTP 429` and nothing more. Unexpected errors return `{"error":"internal_error"}` without a stack trace, and the Docker image doesn't contain any keys.

---

## 6. API behavior

| Code | When |
|---|---|
| 200 | Health is OK, or the optimization succeeded |
| 400 | Malformed JSON or a structurally invalid request: missing fields, anything other than 24 unique hours, 0 or more than 3 notes, a blank note, negative or NaN numbers, wrong types |
| 422 | Well-formed but impossible to schedule, e.g. initial battery energy outside [minimum, capacity], or no feasible plan under the directives |
| 500 | Controlled internal error with no details exposed |

---

## 7. Project layout

```
app/main.py            FastAPI app, endpoints, error handlers, startup warm-up
app/schemas.py         request and response models (Pydantic)
app/llm/prompt.py      system prompt and examples
app/llm/client.py      OpenAI-compatible chat client, JSON extraction
app/llm/interpreter.py LLM call, guardrails, retry, model rotation, fallback tiers, cache
app/llm/rule_parser.py rule-based parser (last resort only)
app/guardrails.py      validation and normalization of directives
app/optimizer.py       linear program (HiGHS), DP fallback, plan assembly
app/validator.py       judge-style replay
app/pipeline.py        request flow and plan_summary
scripts/run_samples.py judge-style check against any running URL
scripts/eval_interpretation.py  interpretation accuracy on paraphrased notes
tests/                 pytest suite
```

---

## 8. Known limitations

- On Groq's free tier, the three rotating models handle roughly 15 to 20 new requests per minute. Past that, requests spill over to the fallback tiers, and in the worst case to the rule parser. A paid Groq tier lifts the limit with no code change.
- Interpretation is only as good as the LLM. The rule parser knows common phrasings and will miss unusual wording.
- Each note maps to one directive, as the problem statement defines. If a note states two constraints, we keep the one the model judges primary.
- When a note leaves out AM or PM, we infer it from context: solar work means daytime, evening peaks mean PM.
- An impossible scenario gets a 422, not a plan that is only partly valid.
- Each worker keeps its own in-memory cache, which empties on every restart.
- Free hosting plans can put the service to sleep after a quiet period, which makes the next request slow. For evaluation, use an always-on instance.

---

## 9. Dependencies and credits

- Python packages: FastAPI, Uvicorn, Pydantic v2, httpx, NumPy and SciPy (HiGHS solver), listed in `requirements.txt`. pytest for the tests.
- LLM providers: Groq API (`openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`) and the Google Gemini API through its OpenAI-compatible endpoint (`gemini-3.5-flash-lite`).
- AI coding assistants were used during development, as the rulebook allows.
