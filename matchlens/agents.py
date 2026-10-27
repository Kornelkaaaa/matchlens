"""Multi-agent narration layer (Microsoft Agent Framework + Microsoft Foundry).

Agents, each with one job, sharing one MatchState (the "shared state"):
  1. Analyst agent      (tools: match snapshot, player stats) → checks the evidence, writes WHY it matters
  2. Narrator agent     → turns analysis into broadcast-ready on-screen copy (title + one line)
  3. Personalizer agent → rewrites for the viewer: mode, club, player, metric, language
  4. Recap agent        → full-match recap and key narratives

Orchestration is explicit and fault-tolerant: every step has a timeout, and if an agent fails (no Azure
config, network, content filter), that step falls back to the deterministic template, so the live overlay
never goes missing. Each overlay records whether it came from the agents or the fallback ("source").

Configuration (environment or .env):
  FOUNDRY_PROJECT_ENDPOINT + FOUNDRY_MODEL           → FoundryChatClient (Azure CLI / managed identity auth)
  or AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY + AZURE_OPENAI_DEPLOYMENT → Azure OpenAI chat client
  neither                                            → template mode (fully offline)
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from . import templates
from .personas import LANGUAGES, Persona
from .stats import MatchState

STEP_TIMEOUT_S = float(os.getenv("MATCHLENS_AGENT_TIMEOUT", "12"))


def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def _base_url(url: str) -> str:
    """Keep only https://host/ (portals sometimes show the endpoint with /openai/v1/ appended)."""
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    return f"{u.scheme}://{u.netloc}/"


def make_client():
    """Return (chat_client, backend_name) or (None, 'templates')."""
    _load_dotenv()
    try:
        if os.getenv("FOUNDRY_PROJECT_ENDPOINT") and os.getenv("FOUNDRY_MODEL"):
            from agent_framework.foundry import FoundryChatClient
            from azure.identity import DefaultAzureCredential
            return FoundryChatClient(project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
                                     model=os.environ["FOUNDRY_MODEL"],
                                     credential=DefaultAzureCredential()), "foundry"
        if os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY"):
            from agent_framework.openai import OpenAIChatCompletionClient
            return OpenAIChatCompletionClient(model=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini"),
                                              azure_endpoint=_base_url(os.environ["AZURE_OPENAI_ENDPOINT"]),
                                              api_key=os.environ["AZURE_OPENAI_API_KEY"],
                                              api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")), "azure-openai"
    except Exception as e:  # missing package or bad config → offline mode, never crash the broadcast
        print(f"[matchlens] agent backend unavailable ({type(e).__name__}: {e}); using templates")
    return None, "templates"


ANALYST = """You are the Analyst agent in a live football broadcast system.
You receive one detected match moment with its evidence (numbers). Use the tools to check the current match
state if useful. Decide WHY the moment matters (tactical, emotional, statistical) using ONLY the evidence and
tool data: never invent players, numbers or events. Reply with JSON:
{"why_it_matters": "<2 sentences max>", "key_numbers": ["...", "..."], "confidence": 0-1}"""

NARRATOR = """You are the Narrator agent writing on-screen broadcast graphics.
Input: a moment + the analyst's reasoning. Output JSON {"title": "<max 8 words>", "body": "<max 25 words>"}.
Title is punchy, body explains why it matters. Only use facts provided."""

PERSONALIZER = """You are the Personalizer agent. Rewrite an on-screen graphic for one specific viewer.
Keep every fact; change tone, depth, framing and language as the viewer profile says. Output JSON
{"title": "<max 8 words>", "body": "<max 30 words>"} in the requested language only."""

RECAP = """You are the Recap agent producing an automated match recap for studio/streaming use.
Use ONLY the facts given (final stats, key moments with evidence, top players). Structure in Markdown:
a headline, a 3-sentence summary, "Key moments" (bullets with minute), "Why it was won/drawn" (2 bullets
tied to numbers), "Player of the match" (1 line with stats). Follow the viewer profile and language."""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


class MatchAgents:
    def __init__(self, state: MatchState):
        self.state = state
        self.client, self.backend = make_client()
        self.failures = 0
        if self.client:
            from agent_framework import Agent

            def get_match_snapshot() -> str:
                """Current score, possession, team stats, momentum and control-vs-chaos for the live match."""
                return json.dumps(self.state.snapshot())

            def get_player_stats(player_id: str) -> str:
                """Live stats for one player by id (e.g. RIV09)."""
                return json.dumps(self.state.player_summary(player_id))

            self.analyst = Agent(self.client, ANALYST, name="analyst", tools=[get_match_snapshot, get_player_stats])
            self.narrator = Agent(self.client, NARRATOR, name="narrator")
            self.personalizer = Agent(self.client, PERSONALIZER, name="personalizer")
            self.recapper = Agent(self.client, RECAP, name="recap", tools=[get_match_snapshot])

    @property
    def live(self) -> bool:
        return self.client is not None and self.failures < 5   # circuit breaker: stop hammering a broken backend

    async def _ask(self, agent, prompt: str) -> str:
        res = await asyncio.wait_for(agent.run(prompt), timeout=STEP_TIMEOUT_S)
        return res.text

    async def _step(self, trace: list, name: str, agent, payload: dict) -> str:
        """Run one agent step and record it in the trace (who ran, how long, ok/failed)."""
        t0 = time.perf_counter()
        try:
            text = await self._ask(agent, json.dumps(payload))
        except Exception as e:
            self.failures += 1
            trace.append({"agent": name, "ms": int((time.perf_counter() - t0) * 1000), "status": "fallback",
                          "note": type(e).__name__})
            print(f"[matchlens] agent step failed ({type(e).__name__}: {str(e)[:300]}); template fallback")
            raise
        trace.append({"agent": name, "ms": int((time.perf_counter() - t0) * 1000), "status": "ok"})
        return text

    async def _personalize(self, draft: dict, persona: Persona, fallback: tuple[str, str], trace: list):
        label = f"personalizer ({persona.mode}, {persona.language})"
        if persona.mode == "analyst" and persona.language == "en" and not persona.club:
            trace.append({"agent": label, "ms": 0, "status": "skipped", "note": "analyst draft already fits"})
            return draft["title"], draft["body"], "agent"
        try:
            final = _parse_json(await self._step(trace, label, self.personalizer,
                                                 {"graphic": draft, "viewer_profile": persona.describe(self.state.meta)}))
            return final["title"], final["body"], "agent"
        except Exception:
            return (*fallback, "template")

    async def narrate_many(self, moment: dict, personas: list[Persona]) -> tuple[list[tuple[str, str, str]], list[dict]]:
        """One moment, many viewers. Analyst + Narrator run once (shared reasoning); the Personalizer then runs
        in parallel for each viewer persona. Every step falls back to templates on its own.
        Returns ([(title, body, source) per persona], trace)."""
        fallbacks = [templates.caption(moment, p, self.state.meta) for p in personas]
        trace: list[dict] = []
        if not self.live:
            trace.append({"agent": "templates", "ms": 0, "status": "offline", "note": "no AI backend: deterministic templates"})
            return [(*fb, "template") for fb in fallbacks], trace
        try:
            analysis = await self._step(trace, "analyst", self.analyst, {"moment": moment})
            try:
                a = _parse_json(analysis)
                trace[-1]["note"] = f"{a.get('why_it_matters', '')} (confidence {a.get('confidence', '?')})"
            except Exception:
                pass
            draft = _parse_json(await self._step(trace, "narrator", self.narrator, {"moment": moment, "analysis": analysis}))
            trace[-1]["note"] = draft.get("title", "")
        except Exception:
            return [(*fb, "template") for fb in fallbacks], trace
        results = await asyncio.gather(*[self._personalize(draft, p, fb, trace) for p, fb in zip(personas, fallbacks)])
        return list(results), trace

    async def narrate(self, moment: dict, persona: Persona) -> tuple[str, str, str]:
        """Return (title, body, source) for a single viewer."""
        (result,), _ = await self.narrate_many(moment, [persona])
        return result

    async def recap(self, moments: list[dict], persona: Persona) -> tuple[str, str]:
        facts = {"final": self.state.snapshot(), "top_players": self.state.top_players(3),
                 "key_moments": [{k: m[k] for k in ("minute", "kind", "headline", "why", "team")}
                                 for m in moments if m["importance"] >= 0.5]}
        if self.live:
            try:
                text = await asyncio.wait_for(self.recapper.run(json.dumps(
                    {"facts": facts, "viewer_profile": persona.describe(self.state.meta)})), timeout=STEP_TIMEOUT_S * 3)
                return text.text, "agent"
            except Exception as e:
                self.failures += 1
                print(f"[matchlens] recap agent failed ({type(e).__name__}: {str(e)[:300]}); template fallback")
        return templates.recap(facts, persona, self.state.meta), "template"
