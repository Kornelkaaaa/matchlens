"""The five-stage pipeline: Ingest → Interpret → Explain → Render → Personalize.

`LiveMatch` replays a synthetic match event-by-event, exactly like a live feed, and yields messages:
  {"type": "tick", ...snapshot}       every few events (scoreboard, ball, momentum)
  {"type": "overlay", ...overlay}     when a moment passes the persona filter (narrated by agents)
  {"type": "end"}
"""
from __future__ import annotations

import asyncio

from .agents import MatchAgents
from .moments import MomentDetector
from .personas import Persona, wants
from .render import overlay
from .stats import MatchState
from .synth import generate

_CACHE: dict[int, dict] = {}


def get_match(seed: int) -> dict:
    if seed not in _CACHE:
        _CACHE[seed] = generate(seed)
    return _CACHE[seed]


def run_offline(seed: int) -> tuple[MatchState, list[dict]]:
    """Process a whole match instantly (for recaps and overlay export)."""
    match = get_match(seed)
    state = MatchState(match["meta"])
    det = MomentDetector(state)
    moments = []
    for ev in match["events"]:
        state.apply(ev)
        moments += [m.to_dict() for m in det.apply(ev)]
    return state, moments


async def export_overlays(seed: int, persona: Persona) -> dict:
    """Full machine-readable overlay timeline for a persona (what a rendering partner would ingest)."""
    state, moments = run_offline(seed)
    agents = MatchAgents(state)
    out = []
    for m in moments:
        if wants(persona, m):
            title, body, src = await agents.narrate(m, persona)
            out.append(overlay(m, title, body, persona.to_dict(), state.meta, src))
    return {"meta": state.meta, "persona": persona.to_dict(), "backend": agents.backend, "overlays": out}


class LiveMatch:
    def __init__(self, seed: int, persona: Persona, speed: float = 60.0, compare: Persona | None = None):
        self.match = get_match(seed)
        self.persona = persona
        self.compare = compare          # optional second viewer: same moment, narrated side by side
        self.speed = max(1.0, speed)
        self.state = MatchState(self.match["meta"])
        self.detector = MomentDetector(self.state)
        self.agents = MatchAgents(self.state)
        self.moments: list[dict] = []

    async def stream(self):
        queue: asyncio.Queue = asyncio.Queue()
        pending: set[asyncio.Task] = set()

        async def narrate(m):
            personas = [self.persona] + ([self.compare] if self.compare else [])
            results, trace = await self.agents.narrate_many(m, personas)
            title, body, src = results[0]
            msg = {"type": "overlay", **overlay(m, title, body, self.persona.to_dict(), self.state.meta, src), "trace": trace}
            if self.compare:
                ct, cb, cs = results[1]
                msg["compare"] = {"title": ct, "body": cb, "source": cs, "persona": self.compare.to_dict()}
            await queue.put(msg)

        async def produce():
            prev_t, last_player_card = 0.0, 0.0
            for i, ev in enumerate(self.match["events"]):
                await asyncio.sleep(min(2.0, max(0.0, (ev["t"] - prev_t) / self.speed)))
                prev_t = ev["t"]
                self.state.apply(ev)
                for m in self.detector.apply(ev):
                    md = m.to_dict()
                    self.moments.append(md)
                    if wants(self.persona, md):
                        task = asyncio.create_task(narrate(md))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                if self.persona.mode == "player" and self.persona.player_id and ev["t"] - last_player_card >= 600:
                    last_player_card = ev["t"]
                    await queue.put({"type": "player_card", "t": ev["t"],
                                     **self.state.player_summary(self.persona.player_id)})
                if i % 3 == 0 or ev["type"] in ("goal", "shot", "half_time", "full_time"):
                    await queue.put({"type": "tick", "event": {k: ev.get(k) for k in
                                     ("type", "team", "player", "x", "y", "end_x", "end_y", "outcome", "minute")},
                                     **self.state.snapshot()})
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await queue.put({"type": "end", "backend": self.agents.backend})

        producer = asyncio.create_task(produce())
        try:
            while True:
                msg = await queue.get()
                yield msg
                if msg["type"] == "end":
                    break
        finally:
            producer.cancel()
