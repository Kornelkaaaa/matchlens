"""MatchLens web API.

Run:  python -m uvicorn matchlens.server:app --reload
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .agents import make_client
from .personas import LANGUAGES, MODES, Persona
from .pipeline import LiveMatch, MatchAgents, export_overlays, get_match, run_offline

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="MatchLens", description="Explainable, personalized match intelligence from synthetic football events")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    _, backend = make_client()
    return {"ok": True, "agent_backend": backend}


@app.get("/api/match")
def match_meta(seed: int = 7):
    m = get_match(seed)
    return {"meta": m["meta"], "events": len(m["events"]), "modes": {k: v["label"] for k, v in MODES.items()},
            "languages": LANGUAGES}


@app.get("/api/events")
def raw_events(seed: int = 7, limit: int = 50):
    """Synthetic event feed (Ingest stage), for inspection."""
    return get_match(seed)["events"][:limit]


@app.get("/api/stream")
async def stream(request: Request, seed: int = 7, speed: float = 60.0):
    persona = Persona.from_query(dict(request.query_params))
    live = LiveMatch(seed, persona, speed)

    async def sse():
        async for msg in live.stream():
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/overlays")
async def overlays(request: Request, seed: int = 7):
    """Full timed overlay JSON for one persona: the machine-readable output for a rendering partner."""
    return JSONResponse(await export_overlays(seed, Persona.from_query(dict(request.query_params))))


@app.get("/api/recap")
async def recap(request: Request, seed: int = 7):
    persona = Persona.from_query(dict(request.query_params))
    state, moments = run_offline(seed)
    text, source = await MatchAgents(state).recap(moments, persona)
    return {"markdown": text, "source": source, "persona": persona.to_dict(), "final": state.snapshot(),
            "top_players": state.top_players(3)}
