"""Stage 2 (Interpret): turn raw events into live match state and football statistics.

`MatchState.apply(event)` is called once per event, in order, exactly as it would be on a live feed.
Everything here is deterministic and explainable: each number can be traced back to the events behind it.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

FINAL_THIRD_X = 70.0
WINDOW_S = 300  # rolling window for momentum / control-vs-chaos (5 match minutes)


def attacking_x(team: str, x: float) -> float:
    """Absolute x → distance from own goal for the team (HOME attacks to x=105)."""
    return x if team == "HOME" else 105.0 - x


def pass_difficulty(ev: dict) -> float:
    """0-10 rating: longer, more forward, pressured passes into the final third are harder."""
    length = ev.get("length_m", 0)
    sx, ex = attacking_x(ev["team"], ev["x"]), attacking_x(ev["team"], ev["end_x"])
    progress = max(0.0, ex - sx)
    score = 1.0 + min(4.0, length / 10) + min(2.5, progress / 12)
    score += 1.5 if ev.get("under_pressure") else 0
    score += 1.0 if ex > FINAL_THIRD_X else 0
    return round(min(10.0, score), 1)


def new_player():
    return {"passes": 0, "passes_ok": 0, "pass_dist": 0.0, "hard_passes_ok": 0, "shots": 0, "goals": 0,
            "xg": 0.0, "tackles_won": 0, "interceptions": 0, "pressures": 0, "distance_m": 0.0,
            "top_speed_kmh": 0.0, "max_shot_speed_kmh": 0.0, "touches": 0}


def new_team():
    return {"passes": 0, "passes_ok": 0, "pass_dist": 0.0, "shots": 0, "on_target": 0, "goals": 0, "xg": 0.0,
            "tackles_won": 0, "interceptions": 0, "pressures": 0, "final_third_entries": 0, "fouls": 0,
            "possession_s": 0.0, "turnovers": 0, "max_shot_speed_kmh": 0.0}


class MatchState:
    def __init__(self, meta: dict):
        self.meta = meta
        self.t = 0.0
        self.minute = 0
        self.score = {"HOME": 0, "AWAY": 0}
        self.teams = {"HOME": new_team(), "AWAY": new_team()}
        self.players: dict[str, dict] = defaultdict(new_player)
        self.player_team: dict[str, str] = {}
        self.player_name: dict[str, str] = {}
        for side, team in meta["teams"].items():
            for p in team["players"]:
                self.player_team[p["id"]] = side
                self.player_name[p["id"]] = p["name"]
        self.in_possession = "HOME"
        self._poss_since = 0.0
        self.window: deque = deque()          # (t, team, kind, value) for rolling metrics
        self.chain = 0                         # current pass chain length
        self.chains: deque = deque(maxlen=40)  # completed chain lengths
        self.last_event: dict | None = None
        self.ball = (52.5, 34.0)

    # ---------- update ----------
    def apply(self, ev: dict) -> None:
        t = ev["t"]
        self.teams[self.in_possession]["possession_s"] += max(0.0, t - self._poss_since)
        self._poss_since = t
        self.t, self.minute, self.last_event = t, ev["minute"], ev
        team, pid = ev["team"], ev.get("player_id")
        T = self.teams[team]
        P = self.players[pid] if pid else None
        if "x" in ev:
            self.ball = (ev.get("end_x", ev["x"]), ev.get("end_y", ev["y"]))
        if P is not None and ev["type"] in ("pass", "carry", "shot"):
            P["touches"] += 1

        etype = ev["type"]
        if etype == "pass":
            ok = ev["outcome"] == "complete"
            T["passes"] += 1; P["passes"] += 1
            T["pass_dist"] += ev["length_m"]; P["pass_dist"] += ev["length_m"]
            ev["difficulty"] = pass_difficulty(ev)
            if ok:
                T["passes_ok"] += 1; P["passes_ok"] += 1
                self.chain += 1
                if ev["difficulty"] >= 7.5:
                    P["hard_passes_ok"] += 1
                if attacking_x(team, ev["x"]) <= FINAL_THIRD_X < attacking_x(team, ev["end_x"]):
                    T["final_third_entries"] += 1
                    self.window.append((t, team, "entry", 1))
            self.window.append((t, team, "pass_ok" if ok else "pass_fail", 1))
        elif etype == "shot":
            T["shots"] += 1; P["shots"] += 1
            T["xg"] += ev["xg"]; P["xg"] += ev["xg"]
            T["max_shot_speed_kmh"] = max(T["max_shot_speed_kmh"], ev["shot_speed_kmh"])
            P["max_shot_speed_kmh"] = max(P["max_shot_speed_kmh"], ev["shot_speed_kmh"])
            if ev["outcome"] in ("goal", "saved"):
                T["on_target"] += 1
            self.window.append((t, team, "xg", ev["xg"]))
        elif etype == "goal":
            T["goals"] += 1; P["goals"] += 1
            self.score = dict(ev["score"])
        elif etype == "tackle" and ev["outcome"] == "won":
            T["tackles_won"] += 1; P["tackles_won"] += 1
        elif etype == "interception":
            T["interceptions"] += 1; P["interceptions"] += 1
        elif etype == "pressure":
            T["pressures"] += 1; P["pressures"] += 1
            self.window.append((t, team, "pressure", 1))
        elif etype == "foul":
            T["fouls"] += 1
        elif etype == "sprint":
            P["distance_m"] += ev["distance_m"]
            P["top_speed_kmh"] = max(P["top_speed_kmh"], ev["speed_kmh"])
        elif etype == "possession_change":
            loser = "AWAY" if team == "HOME" else "HOME"
            self.teams[loser]["turnovers"] += 1
            self.window.append((t, loser, "turnover", 1))
            self.chains.append(self.chain)
            self.chain = 0
            self.in_possession = team
        elif etype == "kickoff":
            self.in_possession = team
            self.chain = 0

        while self.window and self.window[0][0] < t - WINDOW_S:
            self.window.popleft()

    # ---------- derived metrics ----------
    def possession_pct(self) -> dict:
        h, a = self.teams["HOME"]["possession_s"], self.teams["AWAY"]["possession_s"]
        tot = h + a or 1
        return {"HOME": round(100 * h / tot), "AWAY": round(100 * a / tot)}

    def window_sum(self, team: str, kind: str) -> float:
        return sum(v for (_, tm, k, v) in self.window if tm == team and k == kind)

    def dominance(self) -> float:
        """-1 (AWAY dominating) … +1 (HOME dominating) over the last 5 minutes."""
        def share(kind):
            h, a = self.window_sum("HOME", kind), self.window_sum("AWAY", kind)
            return 0.0 if h + a == 0 else (h - a) / (h + a)
        return round(0.4 * share("pass_ok") + 0.3 * share("entry") + 0.3 * share("xg"), 2)

    def control_chaos(self) -> dict:
        """Turnovers per minute (chaos) vs. average completed-pass chain length (control), last 5 min."""
        span = min(WINDOW_S, max(60.0, self.t)) / 60
        turnovers = self.window_sum("HOME", "turnover") + self.window_sum("AWAY", "turnover")
        tpm = turnovers / span
        avg_chain = sum(self.chains) / len(self.chains) if self.chains else 0
        label = "chaos" if tpm >= 4.5 else ("control" if avg_chain >= 4.5 and tpm < 3.0 else "balanced")
        return {"turnovers_per_min": round(tpm, 1), "avg_chain": round(avg_chain, 1), "label": label}

    def team_summary(self, team: str) -> dict:
        T = self.teams[team]
        return {"name": self.meta["teams"][team]["name"], "short": self.meta["teams"][team]["short"],
                "goals": self.score[team], "possession_pct": self.possession_pct()[team],
                "passes": T["passes"], "pass_accuracy_pct": round(100 * T["passes_ok"] / T["passes"]) if T["passes"] else 0,
                "avg_pass_m": round(T["pass_dist"] / T["passes"], 1) if T["passes"] else 0,
                "shots": T["shots"], "on_target": T["on_target"], "xg": round(T["xg"], 2),
                "max_shot_speed_kmh": T["max_shot_speed_kmh"], "tackles_won": T["tackles_won"],
                "interceptions": T["interceptions"], "pressures": T["pressures"],
                "final_third_entries": T["final_third_entries"], "turnovers": T["turnovers"]}

    def player_summary(self, pid: str) -> dict:
        P = self.players[pid]
        return {"id": pid, "name": self.player_name.get(pid, pid), "team": self.player_team.get(pid),
                "passes": P["passes"], "pass_accuracy_pct": round(100 * P["passes_ok"] / P["passes"]) if P["passes"] else 0,
                "hard_passes_ok": P["hard_passes_ok"], "shots": P["shots"], "goals": P["goals"], "xg": round(P["xg"], 2),
                "tackles_won": P["tackles_won"], "interceptions": P["interceptions"], "pressures": P["pressures"],
                "sprint_distance_m": round(P["distance_m"]), "top_speed_kmh": P["top_speed_kmh"],
                "max_shot_speed_kmh": P["max_shot_speed_kmh"], "touches": P["touches"]}

    def top_players(self, n: int = 3) -> list[dict]:
        def impact(pid):
            P = self.players[pid]
            return P["goals"] * 3 + P["xg"] * 2 + P["hard_passes_ok"] * 0.3 + P["tackles_won"] * 0.4 + \
                P["interceptions"] * 0.3 + P["passes_ok"] * 0.02
        return [self.player_summary(pid) for pid in sorted(self.player_name, key=impact, reverse=True)[:n]]

    def snapshot(self) -> dict:
        return {"t": self.t, "minute": self.minute, "score": dict(self.score), "ball": self.ball,
                "in_possession": self.in_possession, "dominance": self.dominance(),
                "control_chaos": self.control_chaos(),
                "teams": {s: self.team_summary(s) for s in ("HOME", "AWAY")}}
