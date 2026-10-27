import asyncio

from matchlens.moments import MomentDetector
from matchlens.personas import Persona, wants
from matchlens.pipeline import export_overlays, run_offline
from matchlens.stats import MatchState, pass_difficulty
from matchlens.synth import generate


def test_generator_is_deterministic():
    assert generate(5)["events"] == generate(5)["events"]


def test_match_is_football_realistic():
    state, _ = run_offline(7)
    for side in ("HOME", "AWAY"):
        t = state.team_summary(side)
        assert 300 <= t["passes"] <= 800
        assert 65 <= t["pass_accuracy_pct"] <= 95
        assert 10 <= t["avg_pass_m"] <= 25
    assert sum(state.score.values()) <= 8


def test_goals_match_score_and_have_evidence():
    state, moments = run_offline(7)
    goals = [m for m in moments if m["kind"] == "goal"]
    assert len(goals) == sum(state.score.values())
    assert all(any("xG" in w for w in g["why"]) for g in goals)


def test_pass_difficulty_bounds():
    ev = {"team": "HOME", "x": 30, "y": 34, "end_x": 85, "end_y": 20, "length_m": 57, "under_pressure": True}
    assert 8 <= pass_difficulty(ev) <= 10


def test_personas_filter_differently():
    _, moments = run_offline(7)
    analyst = [m for m in moments if wants(Persona(mode="analyst"), m)]
    casual = [m for m in moments if wants(Persona(mode="casual"), m)]
    assert len(analyst) > len(casual) > 0


def test_overlay_export_works_offline():
    out = asyncio.run(export_overlays(7, Persona(mode="analyst")))
    assert out["overlays"] and all({"match_t", "slot", "title", "body", "why"} <= o.keys() for o in out["overlays"])


def test_side_by_side_gives_each_viewer_a_story_and_a_trace():
    from matchlens.agents import MatchAgents
    state, moments = run_offline(7)
    goal = next(m for m in moments if m["kind"] == "goal")
    results, trace = asyncio.run(MatchAgents(state).narrate_many(goal, [Persona(mode="analyst"), Persona(mode="kids")]))
    assert len(results) == 2 and all(title and body for title, body, _ in results)
    assert trace and all({"agent", "ms", "status"} <= t.keys() for t in trace)


def test_compare_persona_from_query():
    p = Persona.compare_from_query({"mode": "analyst", "club": "HOME", "cmode": "kids", "clang": "es"})
    assert p.mode == "kids" and p.language == "es" and p.club == "HOME"
    assert Persona.compare_from_query({"mode": "analyst"}) is None


class _FakeAgent:
    """Stands in for a Foundry agent: returns a fixed reply, or raises to simulate an outage."""
    def __init__(self, reply=None, fail=False):
        self.reply, self.fail = reply, fail

    async def run(self, prompt):
        if self.fail:
            raise TimeoutError("simulated outage")
        return type("Res", (), {"text": self.reply})()


def _agents_with_fakes(personalizer_fails=False):
    from matchlens.agents import MatchAgents
    state, moments = run_offline(7)
    agents = MatchAgents(state)
    agents.client = object()   # pretend a backend is configured
    agents.analyst = _FakeAgent('{"why_it_matters": "Big swing.", "key_numbers": [], "confidence": 0.9}')
    agents.narrator = _FakeAgent('{"title": "Northgate take over", "body": "Two goals in five minutes."}')
    agents.personalizer = _FakeAgent('{"title": "Wow, what a goal!", "body": "They scored again!"}', fail=personalizer_fails)
    return agents, next(m for m in moments if m["kind"] == "goal")


def test_agent_handoffs_are_traced():
    agents, goal = _agents_with_fakes()
    (analyst_view, kids_view), trace = asyncio.run(agents.narrate_many(goal, [Persona(mode="analyst"), Persona(mode="kids")]))
    assert analyst_view == ("Northgate take over", "Two goals in five minutes.", "agent")
    assert kids_view == ("Wow, what a goal!", "They scored again!", "agent")
    assert [t["agent"].split(" ")[0] for t in trace] == ["analyst", "narrator", "personalizer", "personalizer"]


def test_failed_personalizer_falls_back_without_breaking_other_steps():
    agents, goal = _agents_with_fakes(personalizer_fails=True)
    (kids_view,), trace = asyncio.run(agents.narrate_many(goal, [Persona(mode="kids")]))
    assert kids_view[2] == "template" and kids_view[0]
    assert trace[-1]["status"] == "fallback" and trace[0]["status"] == "ok"


def test_overlay_export_is_a_time_ordered_timeline():
    out = asyncio.run(export_overlays(7, Persona(mode="analyst")))["overlays"]
    assert [o["match_t"] for o in out] == sorted(o["match_t"] for o in out)
