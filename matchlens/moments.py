"""Stage 3 (Explain): detect key moments and attach the facts that explain WHY they matter.

Each Moment carries structured evidence ("why" facts with numbers). The narration layer (agents or
templates) only turns that evidence into words, so every sentence on screen is traceable to data.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .stats import MatchState, attacking_x

SPEED_ALERT_KMH = 33.0
BIG_CHANCE_XG = 0.30
HARD_PASS = 8.5


@dataclass
class Moment:
    t: float
    minute: int
    kind: str            # goal, big_chance, momentum_swing, pressing_spell, control_spell, chaos_spell,
                         # speed_alert, elite_pass, card, shot_speed, half_time, full_time
    team: str | None
    player: str | None
    player_id: str | None
    importance: float    # 0..1, used to filter per audience
    headline: str
    why: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class MomentDetector:
    def __init__(self, state: MatchState):
        self.s = state
        self.last_dom_side = 0          # -1, 0, +1
        self.last_spell: dict[str, float] = {}
        self.last_flow = "balanced"

    def name(self, team):
        return self.s.meta["teams"][team]["name"] if team else ""

    def cooldown(self, key, secs):
        if self.s.t - self.last_spell.get(key, -1e9) < secs:
            return True
        self.last_spell[key] = self.s.t
        return False

    def apply(self, ev: dict) -> list[Moment]:
        s, out = self.s, []
        t, minute, team = ev["t"], ev["minute"], ev["team"]
        mk = lambda kind, imp, head, why, data=None, tm=team: Moment(
            t, minute, kind, tm, ev.get("player"), ev.get("player_id"), imp, head, why, data or {})

        if ev["type"] == "goal":
            shot = self._last_shot
            why = [f"Score is now {s.score['HOME']}-{s.score['AWAY']}."]
            if shot:
                why.append(f"Shot quality: {shot['xg']:.2f} xG (a goal from that spot happens about "
                           f"{round(shot['xg'] * 100)}% of the time).")
                chain = shot.get("chain_length", 0)
                why.append(f"Shot speed {shot['shot_speed_kmh']:.0f} km/h " + (
                    f"at the end of a {chain}-pass move." if chain >= 3 else "straight after winning the ball: a transition goal."))
                if shot["xg"] < 0.12:
                    why.append("Low-probability finish: individual quality, not a gift.")
            diff = s.score[team] - s.score["AWAY" if team == "HOME" else "HOME"]
            why.append("Takes the lead." if diff == 1 else ("Equaliser." if diff == 0 else
                       ("Extends the lead." if diff > 1 else "Pulls one back.")))
            out.append(mk("goal", 1.0, f"GOAL! {ev['player']} ({self.name(team)})", why,
                          {"score": dict(s.score), "xg": shot["xg"] if shot else None,
                           "shot_speed_kmh": shot["shot_speed_kmh"] if shot else None}))

        elif ev["type"] == "shot":
            self._last_shot = ev
            if ev["outcome"] != "goal" and ev["xg"] >= BIG_CHANCE_XG:
                out.append(mk("big_chance", 0.7, f"Big chance for {ev['player']}",
                              [f"{ev['xg']:.2f} xG: scored {round(ev['xg'] * 100)}% of the time from there.",
                               f"Outcome: {ev['outcome'].replace('_', ' ')}.",
                               f"{self.name(team)} total xG now {s.teams[team]['xg']:.2f} from {s.teams[team]['shots']} shots."],
                              {"xg": ev["xg"], "outcome": ev["outcome"]}))
            elif ev["shot_speed_kmh"] >= 115:
                out.append(mk("shot_speed", 0.45, f"Rocket from {ev['player']}: {ev['shot_speed_kmh']:.0f} km/h",
                              [f"Fastest shot bracket of the match (≥115 km/h).", f"xG {ev['xg']:.2f}."],
                              {"shot_speed_kmh": ev["shot_speed_kmh"]}))

        elif ev["type"] == "pass" and ev["outcome"] == "complete" and ev.get("difficulty", 0) >= HARD_PASS:
            if not self.cooldown(f"pass_{ev['player_id']}", 600):
                out.append(mk("elite_pass", 0.4, f"Elite pass by {ev['player']}",
                              [f"Difficulty {ev['difficulty']}/10: {ev['length_m']:.0f} m"
                               + (" under pressure" if ev.get("under_pressure") else "")
                               + (" into the final third" if attacking_x(team, ev['end_x']) > 70 else "") + ".",
                               f"Ball speed {ev['ball_speed_kmh']:.0f} km/h to {ev['receiver']}."],
                              {"difficulty": ev["difficulty"], "length_m": ev["length_m"]}))

        elif ev["type"] == "sprint" and ev["speed_kmh"] >= SPEED_ALERT_KMH:
            out.append(mk("speed_alert", 0.35, f"{ev['player']} hits {ev['speed_kmh']:.1f} km/h",
                          [f"Sprint of {ev['distance_m']:.0f} m.", f"Threshold for a speed tag: {SPEED_ALERT_KMH} km/h."],
                          {"speed_kmh": ev["speed_kmh"], "distance_m": ev["distance_m"]}))

        elif ev["type"] == "card":
            out.append(mk("card", 0.5, f"{ev['card'].title()} card: {ev['player']}",
                          ["Player now has to defend carefully: a second yellow means a red."]))

        elif ev["type"] in ("half_time", "full_time"):
            out.append(mk(ev["type"], 0.9, f"{'Half-time' if ev['type'] == 'half_time' else 'Full-time'}: "
                          f"{self.name('HOME')} {s.score['HOME']}-{s.score['AWAY']} {self.name('AWAY')}",
                          [], {"snapshot": s.snapshot()}, tm=None))

        # ---- rolling-window moments (checked on every event, rate-limited) ----
        if s.t > 300:
            dom = s.dominance()
            side = 1 if dom >= 0.4 else (-1 if dom <= -0.4 else 0)
            if side and side != self.last_dom_side and not self.cooldown("swing", 600):
                tm = "HOME" if side > 0 else "AWAY"
                opp = "AWAY" if tm == "HOME" else "HOME"
                facts = []  # only the components this team actually leads, so the explanation never contradicts itself
                for kind, label, fmt in (("pass_ok", "completed passes", "{:.0f}"), ("entry", "final-third entries", "{:.0f}"),
                                         ("xg", "xG", "{:.2f}")):
                    a, b = s.window_sum(tm, kind), s.window_sum(opp, kind)
                    if a > b:
                        facts.append(f"{label.capitalize()} in the last 5 min: {fmt.format(a)} vs {fmt.format(b)}.")
                if facts:
                    out.append(mk("momentum_swing", 0.65, f"Momentum shift: {self.name(tm)} take control", facts,
                                  {"dominance": dom}, tm=tm))
            if side:
                self.last_dom_side = side

            for tm in ("HOME", "AWAY"):
                pr = s.window_sum(tm, "pressure")
                if pr >= 22 and not self.cooldown(f"press_{tm}", 600):
                    opp = "AWAY" if tm == "HOME" else "HOME"
                    out.append(mk("pressing_spell", 0.5, f"{self.name(tm)} turn up the press",
                                  [f"{int(pr)} pressures in 5 minutes.",
                                   f"{self.name(opp)} turnovers in that spell: {int(s.window_sum(opp, 'turnover'))}."],
                                  {"pressures": pr}, tm=tm))

            flow = s.control_chaos()
            if flow["label"] != self.last_flow and flow["label"] != "balanced" and not self.cooldown("flow", 480):
                if flow["label"] == "chaos":
                    out.append(mk("chaos_spell", 0.45, "The game has turned chaotic",
                                  [f"{flow['turnovers_per_min']} turnovers per minute (last 5 min).",
                                   "Neither side can string passes together: transitions and mistakes decide this phase."],
                                  flow, tm=None))
                else:
                    holder = "HOME" if s.possession_pct()["HOME"] >= 50 else "AWAY"
                    out.append(mk("control_spell", 0.4, f"{self.name(holder)} slow it down",
                                  [f"Average move length {flow['avg_chain']} passes, only {flow['turnovers_per_min']} "
                                   f"turnovers per minute.", "Patient build-up: control over chaos."], flow, tm=holder))
            self.last_flow = flow["label"]
        return out

    _last_shot: dict | None = None
