"""Deterministic fallback narration (no AI). Also the safety net when an agent step fails.

English only; the multi-language path needs the agents (Personalizer). Every sentence is built from the
moment's own evidence, so the output is always factual.
"""
from __future__ import annotations

from .personas import Persona


def _us(persona: Persona, team: str | None, meta: dict) -> str:
    if team and persona.club == team:
        return "Your team"
    return meta["teams"][team]["name"] if team else ""


def caption(moment: dict, persona: Persona, meta: dict) -> tuple[str, str]:
    kind, why = moment["kind"], moment.get("why", [])
    title = moment["headline"]
    if persona.club and moment.get("team"):
        title = title.replace(meta["teams"][moment["team"]]["name"], _us(persona, moment["team"], meta))
    if persona.mode in ("casual", "kids"):
        body = why[-1] if kind == "goal" and why else (why[0] if why else "")
        if persona.mode == "kids" and kind == "momentum_swing":
            body = "They have the ball more and keep getting close to goal!"
    elif persona.mode == "analyst":
        body = " ".join(why[:3])
    else:  # player focus
        body = " ".join(why[:2])
    return title, body


def recap(facts: dict, persona: Persona, meta: dict) -> str:
    snap = facts["final"]
    H, A = snap["teams"]["HOME"], snap["teams"]["AWAY"]
    hs, as_ = snap["score"]["HOME"], snap["score"]["AWAY"]
    if hs == as_:
        head = f"{H['name']} and {A['name']} share the points, {hs}-{as_}"
    else:
        w, l = (H, A) if hs > as_ else (A, H)
        head = f"{w['name']} beat {l['name']} {max(hs, as_)}-{min(hs, as_)}"
    lines = [f"## {head}", "",
             f"{H['name']} had {H['possession_pct']}% of the ball and {H['shots']} shots ({H['xg']} xG); "
             f"{A['name']} answered with {A['shots']} shots ({A['xg']} xG). "
             f"Pass accuracy: {H['pass_accuracy_pct']}% vs {A['pass_accuracy_pct']}%.", "", "### Key moments"]
    for m in facts["key_moments"]:
        if m["kind"] in ("half_time", "full_time"):
            continue
        lines.append(f"- **{m['minute']}'** {m['headline']}: {' '.join(m['why'][:2])}")
    xg_edge = "HOME" if H["xg"] > A["xg"] else "AWAY"
    lines += ["", "### Why it ended this way",
              f"- Chance quality: {snap['teams'][xg_edge]['name']} created the better chances "
              f"({H['xg']} vs {A['xg']} xG).",
              f"- Territory: final-third entries {H['final_third_entries']} vs {A['final_third_entries']}; "
              f"pressures {H['pressures']} vs {A['pressures']}."]
    if facts["top_players"]:
        p = facts["top_players"][0]
        lines += ["", "### Player of the match",
                  f"**{p['name']}** ({meta['teams'][p['team']]['short']}): {p['goals']} goals, {p['xg']} xG, "
                  f"{p['passes']} passes at {p['pass_accuracy_pct']}%, {p['tackles_won']} tackles won, "
                  f"top speed {p['top_speed_kmh']} km/h."]
    return "\n".join(lines)
