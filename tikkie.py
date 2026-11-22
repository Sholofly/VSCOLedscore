"""Uitwedstrijden via tikkieterug (amateurvoetbal-uitslagen van voetbalnederland.nl).

Bij een thuiswedstrijd komt alles van het scorebord. Bij een uitwedstrijd volgt de
app de wedstrijd van het gekozen team via tikkieterug: het programma van vandaag
wordt af en toe opgehaald, en vanaf een kwartier voor de aftrap wordt de wedstrijd
elke `poll_seconds` bevraagd. Statuswijzigingen en nieuwe doelpunten worden
berichten, met dezelfde sjablonen als bij het scorebord.

De app gebruikt de openbare JSON-API van tikkieterug (dezelfde die de website
gebruikt; geen sleutel nodig): `/clubs?search=`, `/clubs/{id}/programma`,
`/clubs/{id}/uitslagen` en `/matches/{id}?source=`. De MCP-koppeling op
`/mcp` is alleen voor Claude.
"""

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, ContentTypeError

from messages import DEFAULT_TEMPLATES, Message, render_message
from settings import Settings
from state import now as state_now

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 600.0  # zo vaak het programma van vandaag opnieuw ophalen
# Nooit langer dan dit slapen: de watcher kijkt dan opnieuw op de echte klok. Lange
# timers lopen op een klok die stilstaat als de pc (en de Docker-VM) in slaapstand
# gaat; na het wakker worden zou de app dan te laat beginnen met bevragen.
MAX_SLEEP_SECONDS = 60.0
PRE_KICKOFF = timedelta(minutes=15)  # vanaf dan de wedstrijd bevragen
ERROR_RETRY_SECONDS = 60.0
REQUEST_TIMEOUT = 20.0


class TikkieError(Exception):
    """tikkieterug gaf een fout of was niet bereikbaar."""


def clean_name(value: Any) -> str:
    """Namen uit tikkieterug bevatten soms dubbele spaties ("Boaz  Bruijnes")."""
    return " ".join(str(value or "").split())


def known_player(name: str) -> bool:
    return bool(name) and not name.lower().startswith("onbekend")


class TikkieClient:
    """De openbare JSON-API van tikkieterug."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get(self, path: str) -> Any:
        url = self.settings.tikkie_url.rstrip("/") + path
        try:
            async with ClientSession(timeout=ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                async with session.get(url, headers={"Accept": "application/json"}) as response:
                    if response.status != 200:
                        raise TikkieError(f"tikkieterug gaf HTTP {response.status}.")
                    # Een onbekend pad geeft de website (HTML) met status 200: dat is geen antwoord.
                    if "json" not in response.headers.get("Content-Type", ""):
                        raise TikkieError("Onverwacht antwoord van tikkieterug (geen JSON); klopt het adres?")
                    return await response.json()
        except TikkieError:
            raise
        except (ClientError, ContentTypeError, TimeoutError, OSError, ValueError) as error:
            raise TikkieError(f"tikkieterug niet bereikbaar ({type(error).__name__})") from None

    async def search_clubs(self, query: str) -> list[dict]:
        result = await self.get(f"/clubs?search={quote(query)}&active=true")
        clubs = [c for c in result if isinstance(c, dict)] if isinstance(result, list) else []
        return [c for c in clubs if c.get("isActive", True)]  # opgeheven clubs niet tonen

    async def today_match(self, team_id: int, today: date) -> dict | None:
        """De wedstrijd van vandaag van dit team (eerst programma, dan uitslagen)."""
        for kind in ("programma", "uitslagen"):
            days = await self.get(f"/clubs/{int(team_id)}/{kind}")
            for day in days if isinstance(days, list) else []:
                if not isinstance(day, dict) or day.get("date") != today.isoformat():
                    continue
                for match in day.get("matches") or []:
                    if isinstance(match, dict) and team_id in (match.get("homeClubId"), match.get("awayClubId")):
                        return {**match, "date": day["date"]}
        return None

    async def match(self, match_id: int, source: int) -> dict:
        result = await self.get(f"/matches/{int(match_id)}?source={int(source)}")
        if not isinstance(result, dict):
            raise TikkieError("Onverwacht antwoord van tikkieterug.")
        return result


def own_side(match: Mapping[str, Any], team_id: int) -> str:
    return "home" if match.get("homeClubId") == team_id else "away"


class TikkieLifecycle:
    """Maakt van opeenvolgende momentopnames van één wedstrijd de berichten.

    Net als bij het scorebord is de eerste momentopname de beginsituatie (geen
    berichten achteraf). Begin, rust, tweede helft en einde hebben geen minuut;
    een doelpunt heeft de minuut uit tikkieterug.
    """

    def __init__(
        self,
        team_id: int,
        templates: Callable[[], Mapping[str, str]] = lambda: DEFAULT_TEMPLATES,
        own_name: Callable[[], str] = lambda: "",
    ) -> None:
        self.team_id = team_id
        self._templates = templates
        self._own_name = own_name
        self.initialized = False
        self.status: str | None = None
        self.seen_goals: set[tuple[str, int]] = set()

    def _names(self, match: Mapping[str, Any]) -> tuple[str, str]:
        home, away = clean_name(match.get("homeClub")) or "Thuis", clean_name(match.get("awayClub")) or "Uit"
        override = self._own_name().strip()
        if override:
            if own_side(match, self.team_id) == "home":
                home = override
            else:
                away = override
        return home, away

    def _context(self, match: Mapping[str, Any], score: tuple[int, int], minute: str) -> dict[str, Any]:
        home, away = self._names(match)
        side = own_side(match, self.team_id)
        own, opponent = (home, away) if side == "home" else (away, home)
        tag = re.sub(r"[^A-Za-z]", "", opponent)[:3].lower()
        return {
            "minuut": minute,
            "thuis": home,
            "uit": away,
            "eigen": own,
            "tegenstander": opponent,
            "stand": f"{score[0]}-{score[1]}",
            "thuis_score": score[0],
            "uit_score": score[1],
            "hashtag": f"#vsc{tag}",
            "blessuretijd": 0,
        }

    def _render(self, key: str, context: dict[str, Any]) -> list[Message]:
        try:
            text = render_message(self._templates(), key, context)
        except Exception:
            logger.exception("Sjabloon '%s' kon niet worden ingevuld", key)
            text = render_message(DEFAULT_TEMPLATES, key, context)
        return [Message(text, kind=key)] if text else []

    @staticmethod
    def _score(match: Mapping[str, Any]) -> tuple[int, int]:
        def number(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        return number(match.get("homeScore")), number(match.get("awayScore"))

    def _goal_entries(self, match: Mapping[str, Any]) -> list[tuple[tuple[str, int], dict | None, tuple[int, int]]]:
        """Alle doelpunten als ((kant, n-de van die kant), gegevens, stand erna).

        Staat een doelpunt (nog) niet in de lijst maar wel in de stand, dan komt er
        een doelpunt zonder maker en minuut bij.
        """
        entries = []
        running = {"home": 0, "away": 0}
        for goal in match.get("goals") or []:
            if not isinstance(goal, dict) or goal.get("side") not in running:
                continue
            side = goal["side"]
            running[side] += 1
            number = goal.get("teamScore")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                number = running[side]
            entries.append(((side, number), goal, (running["home"], running["away"])))
        score = self._score(match)
        for index, side in enumerate(("home", "away")):
            have = max((key[1] for key, _, _ in entries if key[0] == side), default=0)
            for number in range(have + 1, score[index] + 1):
                entries.append(((side, number), None, score))
        return entries

    def update(self, match: Mapping[str, Any]) -> list[Message]:
        status = match.get("status")
        score = self._score(match)
        context = self._context(match, score, "")
        side = own_side(match, self.team_id)
        events: list[Message] = []
        previous = self.status

        if self.initialized and status != previous:
            if status == "live" and previous in (None, "scheduled"):
                events += self._render("start", context)
            elif status == "halftime":
                if previous in (None, "scheduled"):
                    events += self._render("start", context)
                events += self._render("rest", context)
            elif status == "live" and previous == "halftime":
                events += self._render("second_half", context)

        for key, goal, after in self._goal_entries(match):
            if key in self.seen_goals:
                continue
            self.seen_goals.add(key)
            if not self.initialized or status == "scheduled":
                continue
            minute = goal.get("minute") if goal else None
            minute = str(minute) if isinstance(minute, int) and not isinstance(minute, bool) and minute > 0 else ""
            goal_context = self._context(match, after, minute)
            if key[0] == side:
                player = clean_name(goal.get("player")) if goal else ""
                if known_player(player):
                    events += self._render("goal_home", {**goal_context, "speler": player})
                else:
                    events += self._render("goal_home_no_scorer", goal_context)
            else:
                events += self._render("goal_away", goal_context)

        if self.initialized and status == "ended" and previous != "ended":
            own, opponent = (score[0], score[1]) if side == "home" else (score[1], score[0])
            events += self._render("end_win" if own > opponent else "end_other", context)

        self.status = status
        self.initialized = True
        return events


@dataclass
class TikkieState:
    status: str = "niet ingesteld"
    level: str = "warn"  # ok | warn | error
    source: str = "scoreboard"  # bron van vandaag: scoreboard | tikkieterug
    match_text: str = ""
    last_poll_at: datetime | None = None
    last_error: str = ""
    last_error_at: datetime | None = None
    since: datetime = field(default_factory=state_now)

    def set(self, status: str, level: str = "warn") -> None:
        if (status, level) != (self.status, self.level):
            self.since = state_now()
        self.status, self.level = status, level

    def record_error(self, message: str) -> None:
        self.last_error, self.last_error_at = message, state_now()


def describe(match: Mapping[str, Any]) -> str:
    return (
        f"{clean_name(match.get('homeClub'))} - {clean_name(match.get('awayClub'))}, "
        f"{match.get('date', '')} {match.get('time', '')}".strip()
    )


class TikkieWatcher:
    """Volgt het programma van het team en, bij een uitduel, de wedstrijd zelf."""

    def __init__(
        self,
        settings: Settings,
        publish: Callable[[list[Message]], None],
        client: Any = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.settings = settings
        self.publish = publish
        self.client = client or TikkieClient(settings)
        self.now = now
        self.state = TikkieState()
        self.match: dict | None = None  # de wedstrijd van vandaag, of None
        self._match_checked: tuple[datetime, int | None] | None = None
        self._lifecycle: TikkieLifecycle | None = None
        self._lifecycle_match: int | None = None
        self._finished: set[int] = set()

    # ------------------------------------------------------------- bronkeuze
    def active_source(self) -> str:
        """Welke bron de berichten levert: "scoreboard" of "tikkieterug"."""
        mode = self.settings.source_mode
        if mode == "scoreboard" or not self.settings.tikkie_ready:
            return "scoreboard"
        if mode == "tikkieterug":
            return "tikkieterug"
        match = self.match
        if match is not None and own_side(match, self.settings.team_id) == "away":
            return "tikkieterug"
        return "scoreboard"

    # ------------------------------------------------------------- lus
    async def _wait(self, seconds: float | None) -> bool:
        try:
            await asyncio.wait_for(self.settings.tikkie_changed.wait(), seconds)
        except TimeoutError:
            return False
        return True

    def _kickoff(self, match: Mapping[str, Any]) -> datetime | None:
        try:
            return datetime.strptime(f"{match['date']} {match['time']}", "%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError):
            return None

    async def _refresh_match(self, force: bool) -> None:
        # Op de echte klok (niet de loop-klok): klopt ook na een slaapstand.
        now = self.now()
        team = self.settings.team_id
        checked = self._match_checked
        if (
            not force and checked and checked[1] == team and checked[0].date() == now.date()
            and timedelta(0) <= now - checked[0] < timedelta(seconds=REFRESH_SECONDS)
        ):
            return
        self.match = await self.client.today_match(team, now.date())
        self._match_checked = (now, team)

    def _lifecycle_for(self, match: Mapping[str, Any]) -> TikkieLifecycle:
        match_id = match.get("matchId")
        if self._lifecycle is None or self._lifecycle_match != match_id:
            self._lifecycle = TikkieLifecycle(
                self.settings.team_id,
                lambda: self.settings.templates,
                lambda: self.settings.club_name,
            )
            self._lifecycle_match = match_id
        return self._lifecycle

    async def step(self) -> float | None:
        """Eén ronde; geeft terug hoe lang er gewacht moet worden (None = tot een wijziging)."""
        if not self.settings.tikkie_ready:
            self.match = None
            self.state.source = "scoreboard"
            self.state.set("niet ingesteld (kies een team)", "warn")
            return None
        try:
            await self._refresh_match(force=False)
        except TikkieError as error:
            self.state.record_error(str(error))
            self.state.set("fout bij ophalen programma", "error")
            logger.warning("tikkieterug: %s", error)
            return ERROR_RETRY_SECONDS
        self.state.source = self.active_source()
        match = self.match
        if match is None:
            self.state.match_text = ""
            self.state.set("geen wedstrijd vandaag", "ok")
            return MAX_SLEEP_SECONDS
        self.state.match_text = describe(match)
        if self.state.source != "tikkieterug":
            self.state.set("thuiswedstrijd: het scorebord levert de berichten", "ok")
            return MAX_SLEEP_SECONDS
        if match.get("matchId") in self._finished:
            self.state.set("wedstrijd afgelopen", "ok")
            return MAX_SLEEP_SECONDS
        kickoff = self._kickoff(match)
        if kickoff is not None and self.now() < kickoff - PRE_KICKOFF:
            self.state.set(f"wacht op de aftrap ({match.get('time')})", "ok")
            wait = (kickoff - PRE_KICKOFF - self.now()).total_seconds()
            return max(1.0, min(MAX_SLEEP_SECONDS, wait))
        try:
            detail = await self.client.match(match["matchId"], match.get("source", 1))
        except (TikkieError, KeyError) as error:
            self.state.record_error(str(error))
            self.state.set("fout bij volgen wedstrijd", "error")
            logger.warning("tikkieterug: %s", error)
            return min(ERROR_RETRY_SECONDS, self.settings.poll_seconds * 2)
        detail = {**match, **detail}
        self.publish(self._lifecycle_for(detail).update(detail))
        self.state.last_poll_at = state_now()
        if detail.get("status") == "ended":
            self._finished.add(match.get("matchId"))
            self.state.set("wedstrijd afgelopen", "ok")
            return MAX_SLEEP_SECONDS
        self.state.set("volgt de uitwedstrijd", "ok")
        return float(self.settings.poll_seconds)

    async def run(self) -> None:
        while True:
            self.settings.tikkie_changed.clear()
            try:
                wait = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # nooit de lus laten stoppen
                logger.exception("Onverwachte fout bij tikkieterug")
                self.state.set("onverwachte fout", "error")
                wait = ERROR_RETRY_SECONDS
            if await self._wait(wait):
                self._match_checked = None  # instellingen gewijzigd: programma opnieuw ophalen
