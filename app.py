
"""TripMate v6 local FastAPI API.

Run:
    python app_new.py
"""
from pathlib import Path
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import uvicorn

from backend import run_travel_agent, resume_travel_agent

BASE = Path(__file__).resolve().parent
log = logging.getLogger("tripmate.api")

app = FastAPI(
    title="TripMate AI",
    version="6.0",
)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE / "static")),
    name="static",
)

templates = Jinja2Templates(
    directory=str(BASE / "templates")
)


class PlanRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=3500
    )
    thread_id: str | None = None


class ReviewRequest(BaseModel):
    thread_id: str = Field(
        min_length=1,
        max_length=100
    )
    approved: bool
    feedback: str = Field(
        default="",
        max_length=1000
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "note": (
            "API reachable; MCP availability not tested"
        ),
    }


@app.post("/api/travel")
async def plan(body: PlanRequest):
    try:
        result = await run_in_threadpool(
            run_travel_agent,
            body.message.strip(),
            None,
        )

        return {
            "success": True,
            **result,
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        log.exception("Planning failure")

        raise HTTPException(
            status_code=503,
            detail=(
                "Planning service unavailable; "
                "check server logs and API configuration"
            ),
        ) from exc


@app.post("/api/travel/approve")
async def approve(body: ReviewRequest):
    try:
        result = await run_in_threadpool(
            resume_travel_agent,
            body.thread_id,
            body.approved,
            body.feedback,
        )

        return {
            "success": True,
            **result,
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        log.exception("Approval failure")

        raise HTTPException(
            status_code=503,
            detail=(
                "Approval service unavailable; "
                "status may need checking"
            ),
        ) from exc


if __name__ == "__main__":
    # Development only, bound to localhost.
    # Before public deployment add authentication,
    # user-owned thread authorization, HTTPS,
    # rate limiting and database connection pooling.
    uvicorn.run(
        "app_new:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
