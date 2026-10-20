"""Synthetic, football-realistic match event generator.

Produces a full 90-minute match as a time-ordered list of events (passes, carries, shots, tackles,
interceptions, pressure, fouls, cards, sprints, possession changes). Clubs and players are fictional.
The simulation is seeded, so the same seed always yields the same match (useful for demos and tests).

Coordinates are absolute pitch metres: x in [0, 105] (HOME attacks towards x=105), y in [0, 68].
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

PITCH_L, PITCH_W = 105.0, 68.0
GOAL_Y = PITCH_W / 2

FIRST = ["Leo", "Mateo", "Kai", "Noah", "Elias", "Theo", "Luca", "Oscar", "Felix", "Jonas", "Rafael", "Hugo",
         "Ivan", "Tomas", "Marco", "Samuel", "Adrian", "Nico", "Emil", "Dario", "Yusuf", "Kofi", "Aron", "Bruno"]
LAST = ["Varga", "Okafor", "Lindqvist", "Moreau", "Kowal", "Duarte", "Ferris", "Haddad", "Novak", "Brennan",
        "Sato", "Mensah", "Ricci", "Holm", "Petrov", "Alvarez", "Quinn", "Tanaka", "Weber", "Costa", "Byrne",
        "Silva", "Nkemelu", "Jansen"]

# role, shirt number, home-zone x (metres from own goal), y
FORMATION = [("GK", 1, 5, 34), ("RB", 2, 28, 58), ("CB", 4, 22, 42), ("CB", 5, 22, 26), ("LB", 3, 28, 10),
             ("CM", 6, 45, 34), ("CM", 8, 52, 46), ("AM", 10, 62, 24), ("RW", 7, 75, 58), ("ST", 9, 82, 34),
             ("LW", 11, 75, 10)]

CLUBS = {
    "HOME": {"name": "Riverside Rovers", "short": "RIV", "color": "#d7263d", "strength": 0.54, "press": 0.45},
    "AWAY": {"name": "Northgate Athletic", "short": "NGA", "color": "#1b98e0", "strength": 0.50, "press": 0.55},
}


def make_squads(rng: random.Random) -> dict:
    squads, used = {}, set()
    for side in ("HOME", "AWAY"):
        players = []
        for role, num, hx, hy in FORMATION:
            while True:
                name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                if name not in used:
                    used.add(name)
                    break
            pid = f"{CLUBS[side]['short']}{num:02d}"
            players.append({"id": pid, "name": name, "number": num, "role": role, "hx": hx, "hy": hy,
                            "skill": round(rng.uniform(0.45, 0.8), 2), "top_speed": round(rng.uniform(30.5, 35.5), 1)})
        squads[side] = {**CLUBS[side], "side": side, "players": players}
    return squads


def to_abs(side: str, x: float, y: float) -> tuple[float, float]:
    """Attacking-direction coords (0 = own goal) → absolute pitch coords."""
    return (round(x, 1), round(y, 1)) if side == "HOME" else (round(PITCH_L - x, 1), round(PITCH_W - y, 1))


def xg_for(x: float, y: float) -> float:
    """Simple shot-quality model on attacking-direction coords."""
    dist = math.hypot(PITCH_L - x, GOAL_Y - y)
    angle = abs(math.atan2(7.32 * (PITCH_L - x), (PITCH_L - x) ** 2 + (y - GOAL_Y) ** 2 - (7.32 / 2) ** 2))
    return round(min(0.85, max(0.01, 1 / (1 + math.exp(-(-1.2 - 0.11 * dist + 1.6 * angle))))), 3)


class MatchSimulator:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.seed = seed
        self.squads = make_squads(self.rng)
        self.events: list[dict] = []
        self.t = 0.0
        self.half = 1
        self.score = {"HOME": 0, "AWAY": 0}
        self.momentum = {"HOME": 0.0, "AWAY": 0.0}   # drifts over time, creates spells of dominance

    # ---------- helpers ----------
    def other(self, side):
        return "AWAY" if side == "HOME" else "HOME"

    def emit(self, etype, side, player=None, x=None, y=None, **extra):
        ev = {"id": len(self.events) + 1, "t": round(self.t, 1), "minute": int(self.t // 60) + 1, "half": self.half,
              "type": etype, "team": side, "player_id": player["id"] if player else None,
              "player": player["name"] if player else None}
        if x is not None:
            ev["x"], ev["y"] = to_abs(side, x, y)
        for k, v in extra.items():
            if k in ("end_x", "end_y"):
                continue
            ev[k] = v
        if "end_x" in extra:
            ev["end_x"], ev["end_y"] = to_abs(side, extra["end_x"], extra["end_y"])
        self.events.append(ev)
        return ev

    def power(self, side) -> float:
        return CLUBS[side]["strength"] + self.momentum[side]

    def pick_receiver(self, side, passer, x):
        """Prefer teammates ahead of the ball, but not too far."""
        mates = [p for p in self.squads[side]["players"] if p["id"] != passer["id"] and p["role"] != "GK"]
        weights = []
        for p in mates:
            ahead = p["hx"] - x
            weights.append(max(0.15, 1.6 - abs(ahead - 12) / 18))
        return self.rng.choices(mates, weights)[0]

    def nearest_opponent(self, side, x):
        opp = self.other(side)
        ox = PITCH_L - x
        cands = [p for p in self.squads[opp]["players"] if p["role"] != "GK"]
        return min(cands, key=lambda p: abs(p["hx"] - ox) + self.rng.uniform(0, 14))

    # ---------- simulation ----------
    def run(self, minutes: int = 90) -> dict:
        side, x, y = "HOME", 52.5, 34.0
        holder = next(p for p in self.squads[side]["players"] if p["role"] == "ST")
        self.emit("kickoff", side, holder, x, y)
        half_end = 45 * 60 + self.rng.randint(60, 180)
        full_end = minutes * 60 + self.rng.randint(120, 300)
        last_sprint = 0.0
        chain = 0

        while self.t < full_end:
            if self.half == 1 and self.t >= half_end:
                self.emit("half_time", side)
                self.half, self.t = 2, 45 * 60
                side, x, y = "AWAY", 52.5, 34.0
                holder = next(p for p in self.squads[side]["players"] if p["role"] == "ST")
                self.emit("kickoff", side, holder, x, y)
                chain = 0

            # momentum random walk (bounded): spells where one team dominates
            for s in self.momentum:
                self.momentum[s] = max(-0.12, min(0.12, self.momentum[s] + self.rng.gauss(0, 0.01)))

            # off-ball sprints: player tracking data feeding speed/distance overlays
            if self.t - last_sprint > self.rng.uniform(45, 90):
                sp_side = self.rng.choice(["HOME", "AWAY"])
                sp = self.rng.choice([p for p in self.squads[sp_side]["players"] if p["role"] != "GK"])
                speed = round(min(sp["top_speed"], self.rng.gauss(sp["top_speed"] - 2.5, 1.8)), 1)
                self.emit("sprint", sp_side, sp, sp["hx"], sp["hy"], speed_kmh=speed,
                          distance_m=round(self.rng.uniform(15, 45), 1))
                last_sprint = self.t

            opp = self.other(side)
            press_p = CLUBS[opp]["press"] * (0.6 + x / PITCH_L) * 0.55
            under_pressure = self.rng.random() < press_p
            if under_pressure:
                presser = self.nearest_opponent(side, x)
                self.emit("pressure", opp, presser, PITCH_L - x, PITCH_W - y, on_player=holder["name"])

            shoot_p = 0.0 if x < 72 else (0.025 if x < 88 else 0.16) * (0.7 + holder["skill"])
            r = self.rng.random()

            if r < shoot_p:  # ---- shot
                xg = xg_for(x, y)
                speed = round(max(45, self.rng.gauss(96 if x < 88 else 82, 14)), 1)
                goal = self.rng.random() < xg * (0.85 + self.power(side) - 0.5)
                on_target = goal or self.rng.random() < 0.38
                outcome = "goal" if goal else ("saved" if on_target else self.rng.choice(["off_target", "blocked"]))
                self.emit("shot", side, holder, x, y, end_x=PITCH_L, end_y=GOAL_Y + self.rng.uniform(-6, 6),
                          xg=xg, shot_speed_kmh=speed, outcome=outcome, under_pressure=under_pressure,
                          chain_length=chain)
                self.t += self.rng.uniform(3, 6)
                if goal:
                    self.score[side] += 1
                    self.emit("goal", side, holder, x, y, score=dict(self.score))
                    self.momentum[side] += 0.03
                    self.momentum[opp] += 0.05  # trailing team pushes
                    self.t += self.rng.uniform(45, 75)
                    side, x, y = opp, 52.5, 34.0
                    holder = next(p for p in self.squads[side]["players"] if p["role"] == "ST")
                    self.emit("kickoff", side, holder, x, y)
                else:
                    side = opp
                    gk = next(p for p in self.squads[side]["players"] if p["role"] == "GK")
                    holder, x, y = gk, 6.0, 34.0
                    self.emit("possession_change", side, holder, x, y, reason="shot_" + outcome)
                    self.t += self.rng.uniform(5, 12)
                chain = 0
                continue

            if r < shoot_p + 0.80:  # ---- pass
                receiver = self.pick_receiver(side, holder, x)
                tx = min(103, max(3, x + max(-15, min(32, (receiver["hx"] - x) * 0.45 + self.rng.gauss(3, 7)))))
                ty = min(66, max(2, y + (receiver["hy"] - y) * 0.5 + self.rng.gauss(0, 6)))
                length = math.hypot(tx - x, ty - y)
                progress = tx - x
                p_ok = (0.99 - 0.0055 * length - (0.14 if under_pressure else 0) - (0.08 if progress > 15 else 0)
                        - (0.07 if tx > 85 else 0) + (holder["skill"] - 0.6) * 0.25 + (self.power(side) - 0.5) * 0.5)
                ok = self.rng.random() < p_ok
                self.emit("pass", side, holder, x, y, end_x=tx, end_y=ty, receiver=receiver["name"],
                          receiver_id=receiver["id"], outcome="complete" if ok else "incomplete",
                          length_m=round(length, 1), under_pressure=under_pressure,
                          ball_speed_kmh=round(min(110, 25 + length * 1.7 + self.rng.gauss(0, 6)), 1))
                self.t += self.rng.uniform(2.5, 5.5)
                if ok:
                    holder, x, y = receiver, tx, ty
                    chain += 1
                else:
                    winner = self.nearest_opponent(side, tx)
                    how = "interception" if self.rng.random() < 0.35 else "ball_recovery"
                    self.emit(how, opp, winner, PITCH_L - tx, PITCH_W - ty)
                    self.emit("possession_change", opp, winner, PITCH_L - tx, PITCH_W - ty, reason=how)
                    side, holder, x, y, chain = opp, winner, PITCH_L - tx, PITCH_W - ty, 0
                continue

            # ---- carry, possibly challenged
            nx = min(102, x + self.rng.uniform(4, 14))
            ny = min(66, max(2, y + self.rng.gauss(0, 5)))
            self.emit("carry", side, holder, x, y, end_x=nx, end_y=ny, length_m=round(math.hypot(nx - x, ny - y), 1))
            self.t += self.rng.uniform(2, 4.5)
            x, y = nx, ny
            if self.rng.random() < 0.22:
                tackler = self.nearest_opponent(side, x)
                won = self.rng.random() < 0.5 + (self.power(opp) - self.power(side))
                if self.rng.random() < 0.12:  # foul
                    card = "yellow" if self.rng.random() < 0.18 else None
                    self.emit("foul", opp, tackler, PITCH_L - x, PITCH_W - y, on_player=holder["name"], card=card)
                    if card:
                        self.emit("card", opp, tackler, PITCH_L - x, PITCH_W - y, card=card)
                    self.t += self.rng.uniform(20, 40)
                    continue
                self.emit("tackle", opp, tackler, PITCH_L - x, PITCH_W - y, on_player=holder["name"],
                          outcome="won" if won else "lost")
                if won:
                    self.emit("possession_change", opp, tackler, PITCH_L - x, PITCH_W - y, reason="tackle")
                    side, holder, x, y, chain = opp, tackler, PITCH_L - x, PITCH_W - y, 0

        self.emit("full_time", side, score=dict(self.score))
        return {"meta": self.meta(), "events": self.events}

    def meta(self) -> dict:
        teams = {s: {k: v for k, v in sq.items() if k not in ("strength", "press")} for s, sq in self.squads.items()}
        return {"seed": self.seed, "competition": "Synthetic League", "venue": "Synthetic Park",
                "teams": teams, "final_score": dict(self.score), "pitch": {"length": PITCH_L, "width": PITCH_W}}


def generate(seed: int = 7) -> dict:
    return MatchSimulator(seed).run()


if __name__ == "__main__":
    import sys
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    match = generate(seed)
    out = Path(__file__).resolve().parent.parent / "data" / f"match_{seed}.json"
    out.write_text(json.dumps(match, indent=1), encoding="utf-8")
    m = match["meta"]
    print(f"{m['teams']['HOME']['name']} {m['final_score']['HOME']}-{m['final_score']['AWAY']} "
          f"{m['teams']['AWAY']['name']} · {len(match['events'])} events → {out}")
