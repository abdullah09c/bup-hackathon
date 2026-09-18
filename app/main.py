"""GridWise LLM API: GET /health, POST /optimize-energy."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import load_settings
from .llm import client
from .optimizer import warm_up as warm_up_solver
from .pipeline import ScenarioError, run
from .schemas import OptimizeRequest, OptimizeResponse

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gridwise")

settings = load_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.endpoints:
        log.info("LLM endpoints: %s", [e.name for e in settings.endpoints])
    else:
        log.warning("No LLM configured (LLM_API_KEY unset): using stub rule-based interpreter. "
                    "Set LLM_PROVIDER/LLM_API_KEY before deployment.")
    # Warm-up so the first judged request is not slow: load SciPy/HiGHS now (~1 s, before
    # the port opens) and open the TLS connections to the LLM hosts in the background.
    await run_in_threadpool(warm_up_solver)
    llm_warmup = asyncio.create_task(client.warm_up(settings.endpoints))
    yield
    llm_warmup.cancel()
    await client.close()


app = FastAPI(title="GridWise LLM", version="1.0.1", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _bad_request(_: Request, exc: RequestValidationError):
    details = []
    for e in exc.errors()[:10]:
        loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
        details.append({"field": loc or "body", "message": e.get("msg", "invalid")})
    return JSONResponse(status_code=400, content={"error": "invalid_request", "details": details})


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


@app.exception_handler(Exception)
async def _internal(_: Request, exc: Exception):
    log.error("internal error: %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"error": "internal_error"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(req: OptimizeRequest):
    try:
        return await run(req, settings)
    except ScenarioError as exc:
        return JSONResponse(status_code=422, content={"error": "unprocessable_scenario", "message": str(exc)})
