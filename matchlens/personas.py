"""Stage 5 (Personalize): who is watching, and what they get.

The same moments feed every persona; personas decide what passes the filter, how much data is shown,
which side is "us", and which language the agents write in.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

LANGUAGES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German",
             "pl": "Polish", "ar": "Arabic", "ja": "Japanese", "hi": "Hindi", "sw": "Swahili"}

MODES = {
    "casual": {"label": "Casual fan", "min_importance": 0.6, "numbers": "few",
               "style": "Warm, simple, exciting. No jargon. One short sentence of context, max 2 numbers."},
    "analyst": {"label": "Analyst", "min_importance": 0.3, "numbers": "many",
                "style": "Precise and tactical. Use the exact metrics given. Explain cause and effect."},
    "player": {"label": "Player focus", "min_importance": 0.0, "numbers": "some",
               "style": "Follow one player: what they did and why it mattered. Personal, vivid."},
    "kids": {"label": "Young fan", "min_importance": 0.6, "numbers": "few",
             "style": "Very simple words for a 9-year-old. Positive, explains football ideas plainly."},
}


@dataclass
class Persona:
    mode: str = "casual"            # casual | analyst | player | kids
    club: str | None = None         # "HOME" | "AWAY" | None (neutral)
    player_id: str | None = None    # for player focus
    language: str = "en"
    metric: str | None = None       # optional single metric a viewer cares about, e.g. "xg", "speed", "passing"

    @classmethod
    def from_query(cls, q: dict) -> "Persona":
        p = cls(mode=q.get("mode", "casual"), club=q.get("club") or None, player_id=q.get("player") or None,
                language=q.get("lang", "en"), metric=q.get("metric") or None)
        if p.mode not in MODES:
            p.mode = "casual"
        if p.language not in LANGUAGES:
            p.language = "en"
        return p

    @classmethod
    def compare_from_query(cls, q: dict) -> "Persona | None":
        """Second viewer for side-by-side mode (cmode/clang); shares club, player and metric with the first."""
        if q.get("cmode") not in MODES:
            return None
        base = cls.from_query(q)
        lang = q.get("clang", "en")
        return cls(mode=q["cmode"], club=base.club, player_id=base.player_id,
                   language=lang if lang in LANGUAGES else "en", metric=base.metric)

    def to_dict(self):
        return asdict(self)

    def describe(self, meta: dict) -> str:
        m = MODES[self.mode]
        bits = [f"Audience: {m['label']}. Style: {m['style']}", f"Write in {LANGUAGES[self.language]}."]
        if self.club:
            bits.append(f"The viewer supports {meta['teams'][self.club]['name']}: frame events from their side "
                        f"('your team'), stay fair to the opponent.")
        if self.player_id:
            bits.append(f"The viewer follows player {self.player_id}.")
        if self.metric:
            bits.append(f"The viewer especially cares about: {self.metric}.")
        return " ".join(bits)


METRIC_KINDS = {"xg": {"big_chance", "goal"}, "speed": {"speed_alert", "shot_speed"},
                "passing": {"elite_pass", "control_spell"}, "pressing": {"pressing_spell", "chaos_spell"}}


def wants(persona: Persona, moment: dict) -> bool:
    """Does this persona get an overlay for this moment?"""
    kind, imp = moment["kind"], moment["importance"]
    if kind in ("goal", "half_time", "full_time"):
        return True
    if persona.mode == "player":
        return moment.get("player_id") == persona.player_id
    if persona.metric and kind in METRIC_KINDS.get(persona.metric, set()):
        return True
    if persona.club and moment.get("team") == persona.club and imp >= MODES[persona.mode]["min_importance"] - 0.15:
        return True
    return imp >= MODES[persona.mode]["min_importance"]
