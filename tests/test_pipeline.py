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
