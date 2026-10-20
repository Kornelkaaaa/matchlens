"""Stage 4 (Render): turn a narrated moment into a timed, machine-readable broadcast overlay.

Overlay schema (one JSON object per graphic), designed so a rendering partner can drop it straight onto a
live feed:
  id, match_t (seconds), minute, duration_s, slot, priority, team, color, title, body, why[], data{}, lang,
  persona, source ("agent" | "template")
Slots: "scorebug_alert" (goals/periods), "lower_third" (stories), "player_tag" (speed/name tags),
"stat_card" (numbers panel).
"""
from __future__ import annotations

SLOTS = {"goal": ("scorebug_alert", 12, 100), "half_time": ("scorebug_alert", 15, 90), "full_time": ("scorebug_alert", 20, 90),
         "big_chance": ("lower_third", 8, 70), "momentum_swing": ("lower_third", 10, 60),
         "pressing_spell": ("lower_third", 8, 50), "chaos_spell": ("lower_third", 8, 45), "control_spell": ("lower_third", 8, 40),
         "card": ("lower_third", 6, 55), "elite_pass": ("stat_card", 6, 35), "shot_speed": ("stat_card", 6, 40),
         "speed_alert": ("player_tag", 5, 30)}


def overlay(moment: dict, title: str, body: str, persona: dict, meta: dict, source: str) -> dict:
    slot, duration, priority = SLOTS.get(moment["kind"], ("lower_third", 6, 30))
    team = moment.get("team")
    return {"id": f"{moment['kind']}-{int(moment['t'])}", "match_t": moment["t"], "minute": moment["minute"],
            "duration_s": duration, "slot": slot, "priority": priority, "kind": moment["kind"], "team": team,
            "color": meta["teams"][team]["color"] if team else "#f5b700", "title": title, "body": body,
            "why": moment.get("why", []), "data": moment.get("data", {}), "lang": persona.get("language", "en"),
            "persona": persona.get("mode"), "source": source}
