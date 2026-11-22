"""Bron: het scorebord (ledscores-websocket).

Het scorebord stuurt bij elke wijziging de volledige status. `MatchLifecycle`
maakt daar berichten van; `Connector` houdt de verbinding open (reconnect,
ping). Je club is de thuisploeg op het scorebord.
"""

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosedOK,
    InvalidStatus,
    WebSocketException,
)

from messages import DEFAULT_TEMPLATES, Message, default_hashtag, render_message
from settings import Settings, token_expiry
from state import ConnectionState, now as state_now

from .base import Publisher, Source, StatusRow, fmt_time

logger = logging.getLogger(__name__)
# Ruwe berichten gaan alleen naar een eigen logbestand (zie app.setup_raw_log).
raw_logger = logging.getLogger("matchshare.raw")
raw_logger.propagate = False  # ruwe berichten nooit naar de console

GOAL_WAIT_SECONDS = 20.0  # standaard wachttijd op de doelpuntmaker (instelbaar in de webapp)
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def redact_token(message: str, token: str) -> str:
    if not token:
        return message
    return message.replace(token, "[redacted]").replace(
        quote(token, safe=""), "[redacted]"
    )



class MatchLifecycle:
    def __init__(
        self,
        regular_minutes: int = 90,
        templates: Mapping[str, str] | Callable[[], Mapping[str, str]] | None = None,
        goal_wait: Callable[[], float] | None = None,
        hashtag: Callable[[], str] | None = None,
        on_match_end: Callable[[], None] | None = None,
    ) -> None:
        self.regular_minutes = regular_minutes
        # Per doelpunt opgevraagd, zodat een wijziging in de webapp direct geldt.
        self._goal_wait: Callable[[], float] = goal_wait or (lambda: GOAL_WAIT_SECONDS)
        # Eigen hashtag voor deze wedstrijd (leeg = standaard); na het einde wordt die gewist.
        self._hashtag: Callable[[], str] = hashtag or (lambda: "")
        self._on_match_end: Callable[[], None] = on_match_end or (lambda: None)
        if templates is None:
            self._templates: Callable[[], Mapping[str, str]] = lambda: DEFAULT_TEMPLATES
        elif callable(templates):
            self._templates = templates
        else:
            self._templates = lambda: templates
        self.initialized = False
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.active = False
        self.rest = False
        self.score: tuple[Any, Any] | None = None
        # Thuis- en uitploeg van de laatst ontvangen status (None = nog niets ontvangen)
        self.teams: tuple[str, str] | None = None
        # Doelpuntmakers: (kant, positie, naam) -> foto-URL (of None)
        self.scorers: dict[tuple[str, int, str], str | None] = {}
        # Eigen doelpunt dat wacht op een doelpuntmaker: (context, deadline)
        self.pending_goal: tuple[dict[str, Any], float] | None = None

    @property
    def next_deadline(self) -> float | None:
        return self.pending_goal[1] if self.pending_goal else None

    def _render(self, key: str, context: dict[str, Any]) -> list[Message]:
        try:
            text = render_message(self._templates(), key, context)
        except Exception:  # een kapot sjabloon mag de verwerking niet stoppen
            logger.exception("Sjabloon '%s' kon niet worden ingevuld", key)
            text = render_message(DEFAULT_TEMPLATES, key, context)
        return [Message(text, kind=key)] if text else []

    def flush_pending(self) -> list[str]:
        """Meld een wachtend doelpunt zonder doelpuntmaker."""
        if self.pending_goal is None:
            return []
        context, _ = self.pending_goal
        self.pending_goal = None
        return self._render("goal_home_no_scorer", context)

    def flush_expired(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        deadline = self.next_deadline
        return self.flush_pending() if deadline is not None and now >= deadline else []

    @staticmethod
    def _score_changes(
        previous: tuple[Any, Any] | None, current: tuple[Any, Any]
    ) -> tuple[str | None, bool]:
        """Geef (kant met hogere stand, of de stand ergens lager is geworden)."""
        scored_side, decreased = None, False
        if previous is None:
            return scored_side, decreased
        for side, old, new in zip(("home", "away"), previous, current):
            if isinstance(old, int) and isinstance(new, int):
                if new > old and scored_side is None:
                    scored_side = side
                decreased = decreased or new < old
        return scored_side, decreased

    def update(self, payload: dict[str, Any], now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        if not isinstance(payload, dict):
            return []
        data = payload.get("scoreboard_data")
        if not isinstance(data, dict):
            return []

        started_at = data.get("startedAt")
        ended_at = data.get("endedAt")
        is_active = bool(started_at) and not bool(ended_at)
        is_rest = bool(data.get("rest"))
        home, away = self._team_names(payload, data)
        self.teams = (home, away)
        scoreboard = data.get("scoreboard")
        scoreboard = scoreboard if isinstance(scoreboard, dict) else {}
        score = (scoreboard.get("home"), scoreboard.get("away"))
        context = self._context(home, away, score, self._minute_label(data))
        scorers = self._scorers(data)
        events: list[str] = []

        if self.initialized:
            started = is_active and (
                not self.active or started_at != self.started_at
            )
            ended = bool(ended_at) and not bool(self.ended_at)
            scored_side, decreased = self._score_changes(self.score, score)
            if started or ended:
                events.extend(self.flush_pending())
            if decreased:
                self.pending_goal = None  # scorecorrectie: doelpunt vervalt

            if started:
                events.extend(self._render("start", context))
            if is_active and is_rest and not self.rest:
                events.extend(self._render("rest", context))
            if is_active and self.rest and not is_rest and not started:
                events.extend(self._render("second_half", context))
            if ended:
                events.extend(self._end_messages(self._elapsed_seconds(data), score, context))
                self._on_match_end()  # een eigen hashtag gold alleen voor deze wedstrijd
            if is_active and not started and scored_side == "away":
                events.extend(self._render("goal_away", context))
            elif is_active and not started and scored_side == "home":
                events.extend(self.flush_pending())
                self.pending_goal = (context, now + self._goal_wait())
            # Alleen de doelpuntmaker van de eigen (thuis)ploeg wordt gebruikt.
            for key in sorted(scorers.keys() - self.scorers.keys()):
                side, _, name = key
                if side == "home" and self.pending_goal:
                    goal_context, _ = self.pending_goal
                    self.pending_goal = None
                    events.extend(
                        Message(text, scorers[key], "goal_home")
                        for text in self._render("goal_home", {**goal_context, "speler": name})
                    )

        self.started_at = started_at
        self.ended_at = ended_at
        self.active = is_active
        self.rest = is_rest
        self.score = score
        self.scorers = scorers
        self.initialized = True
        return events

    def _context(
        self, home: str, away: str, score: tuple[Any, Any], minute: str
    ) -> dict[str, Any]:
        home_score = "?" if score[0] is None else score[0]
        away_score = "?" if score[1] is None else score[1]
        return {
            "minuut": minute,
            "thuis": home,
            "uit": away,
            # Bij het scorebord speelt de eigen club altijd thuis.
            "eigen": home,
            "tegenstander": away,
            "stand": f"{home_score}-{away_score}",
            "thuis_score": home_score,
            "uit_score": away_score,
            "hashtag": self._hashtag() or default_hashtag(home, away),
            "blessuretijd": 0,
        }

    def _end_messages(
        self, seconds: Any, score: tuple[Any, Any], context: dict[str, Any]
    ) -> list[str]:
        extra = 0
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            minute = "?"
        else:
            played = math.ceil(seconds / 60)  # 90:00 is 90, 92:01 is 90+3
            extra = max(0, played - self.regular_minutes)
            minute = str(min(played, self.regular_minutes))
            if extra:
                minute += f"+{extra}"
        home_score, away_score = score
        won = (
            isinstance(home_score, int)
            and isinstance(away_score, int)
            and home_score > away_score
        )
        context = {**context, "minuut": minute, "blessuretijd": extra}
        return self._render("end_win" if won else "end_other", context)

    def _elapsed_seconds(self, data: dict[str, Any]) -> float | None:
        """Gespeelde seconden. `time` begint elke helft opnieuw bij 0 (period 0/1)."""
        seconds = data.get("time")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None
        period = data.get("period")
        second_half = isinstance(period, int) and not isinstance(period, bool) and period >= 1
        return seconds + (self.regular_minutes * 30 if second_half else 0)

    def _minute(self, data: dict[str, Any]) -> int | None:
        """Speelminuut: 0:05 is minuut 1, 45:30 is minuut 46; bij rust de eindminuut."""
        seconds = self._elapsed_seconds(data)
        if seconds is None:
            return None
        if data.get("rest"):  # tijdens rust staat `time` op 0
            period = data.get("period")
            second_half = isinstance(period, int) and not isinstance(period, bool) and period >= 1
            return self.regular_minutes // 2 * (2 if second_half else 1)
        return int(seconds // 60) + 1

    def _minute_label(self, data: dict[str, Any]) -> str:
        """Minuut als tekst, met blessuretijd per helft: 45:30 in de eerste helft is "45+1",
        in de tweede helft is na 90:00 "90+1" (zoals bij het eindbericht)."""
        minute = self._minute(data)
        if minute is None:
            return "?"
        period = data.get("period")
        second_half = isinstance(period, int) and not isinstance(period, bool) and period >= 1
        limit = self.regular_minutes if second_half else self.regular_minutes // 2
        return f"{limit}+{minute - limit}" if minute > limit else str(minute)

    @staticmethod
    def _scorers(data: dict[str, Any]) -> dict[tuple[str, int, str], str | None]:
        """Doelpuntmakers als (kant, positie, naam) -> foto-URL; de lijst kan null bevatten.

        De foto is `goalImage` (doelpuntfoto), anders `image` (pasfoto), alleen als http(s)-URL.
        """
        raw = data.get("scorers")
        result: dict[tuple[str, int, str], str | None] = {}
        if not isinstance(raw, dict):
            return result
        for side in ("home", "away"):
            entries = raw.get(side)
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                player = entry.get("player") if isinstance(entry, dict) else None
                name = player.get("name") if isinstance(player, dict) else None
                if name:
                    photo = None
                    for field in ("goalImage", "image"):
                        value = player.get(field)
                        if isinstance(value, str) and value.startswith(("https://", "http://")):
                            photo = value
                            break
                    result[(side, index, str(name))] = photo
        return result

    @staticmethod
    def _team_names(payload: dict[str, Any], data: dict[str, Any]) -> tuple[str, str]:
        names = {
            team.get("id"): team.get("scoreboard_name") or team.get("name")
            for team in payload.get("teams", [])
            if isinstance(team, dict)
        }
        team_ids = data.get("teams")
        team_ids = team_ids if isinstance(team_ids, dict) else {}
        return (
            str(names.get(team_ids.get("home")) or "Thuis"),
            str(names.get(team_ids.get("away")) or "Uit"),
        )


def build_websocket_uri(endpoint: str, token: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"ws", "wss"} or not parts.netloc:
        raise ValueError("WS_URL must be a valid ws:// or wss:// URL.")
    if not token.strip():
        raise ValueError("WS_TOKEN is empty. Set it in the .env file.")

    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "token"]
    query.append(("token", token))
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


class Connector:
    """Houdt de verbinding open: foutafhandeling, reconnect met oplopende wachttijd."""

    INITIAL_DELAY = 1.0
    MAX_DELAY = 60.0
    STABLE_SECONDS = 30.0  # zo lang verbonden = wachttijd weer terug naar het begin
    # Elke PING_INTERVAL seconden een ping; zonder antwoord binnen PING_TIMEOUT geldt de
    # verbinding als verbroken. Zo valt een weggevallen internetverbinding binnen ~20 s op
    # (zonder ping merkt een TCP-verbinding dat soms pas na minuten).
    PING_INTERVAL = 10.0
    PING_TIMEOUT = 10.0
    # Zonder internet komt er ook geen nette afsluiting van de server: niet lang wachten
    # (standaard 10 s), anders blijft de status nog die tijd op "verbonden" staan.
    CLOSE_TIMEOUT = 2.0

    def __init__(
        self,
        settings: Settings,
        state: ConnectionState,
        regular_minutes: int = 90,
        connect_func: Callable[..., Any] = connect,
        publish: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        # Waar de berichten heen gaan (standaard: alleen de lijst in de webomgeving).
        self.publish = publish or Publisher(state).publish
        self.regular_minutes = regular_minutes
        self._connect = connect_func
        self._lifecycle: MatchLifecycle | None = None
        self._lifecycle_url = ""
        self._connected_at: float | None = None

    def _lifecycle_for(self, url: str) -> MatchLifecycle:
        # Bij een nieuwe URL begint de beginsituatie opnieuw; bij een reconnect
        # blijft de status bewaard, zodat gemiste wijzigingen alsnog worden gemeld.
        if self._lifecycle is None or self._lifecycle_url != url:
            self._lifecycle = MatchLifecycle(
                self.regular_minutes,
                lambda: self.settings.templates,
                goal_wait=lambda: self.settings.goal_wait_seconds,
                hashtag=lambda: self.settings.hashtag_override,
                # Het scorebord draait alleen als het de gekozen bron is, dus een
                # eindsignaal hoort bij de wedstrijd waarvoor de eigen hashtag gold.
                on_match_end=self.settings.clear_hashtag,
            )
            self._lifecycle_url = url
        return self._lifecycle

    async def _wait(self, timeout: float | None) -> bool:
        """Wacht tot de timeout of tot URL/token wijzigt. Geeft True bij een wijziging."""
        try:
            await asyncio.wait_for(self.settings.connection_changed.wait(), timeout)
        except TimeoutError:
            return False
        return True

    @property
    def teams(self) -> tuple[str, str] | None:
        """Thuis- en uitploeg op het scorebord, als er al een status is ontvangen."""
        return self._lifecycle.teams if self._lifecycle is not None else None

    def _publish(self, events: list[str]) -> None:
        self.publish(events)

    async def run(self) -> None:
        delay = self.INITIAL_DELAY
        while True:
            self.settings.connection_changed.clear()
            url, token = self.settings.url, self.settings.token
            try:
                uri = build_websocket_uri(url, token)
            except ValueError:
                detail = "URL of token ontbreekt. Vul ze in via de webomgeving."
                self.state.set("niet geconfigureerd", "error", detail)
                logger.error("Niet verbonden: %s", detail)
                await self._wait(None)
                delay = self.INITIAL_DELAY
                continue

            lifecycle = self._lifecycle_for(url)
            self.state.attempt += 1
            self.state.set("verbinden", "warn")
            self._connected_at = None
            outcome, error_text, auth_failed = "closed", "", False
            try:
                outcome = await self._session(uri, token, lifecycle)
            except asyncio.CancelledError:
                self._publish(lifecycle.flush_pending())
                raise
            except InvalidStatus as error:
                code = error.response.status_code
                auth_failed = code in (401, 403)
                error_text = (
                    f"Server weigerde de verbinding (HTTP {code}). "
                    + ("Het token is ongeldig of verlopen." if auth_failed else "")
                ).strip()
            except (OSError, TimeoutError, WebSocketException) as error:
                error_text = redact_token(f"{type(error).__name__}: {error}", token)
            except Exception as error:  # onverwacht: nooit de lus laten stoppen
                logger.exception("Onverwachte fout in de verbinding")
                error_text = redact_token(f"{type(error).__name__}: {error}", token)
            self._publish(lifecycle.flush_pending())

            if outcome == "changed":
                logger.info("URL of token gewijzigd; opnieuw verbinden")
                delay = self.INITIAL_DELAY
                continue

            uptime = 0.0 if self._connected_at is None else time.monotonic() - self._connected_at
            if uptime >= self.STABLE_SECONDS:
                delay = self.INITIAL_DELAY
            wait = self.MAX_DELAY if auth_failed else delay
            wait *= random.uniform(0.9, 1.1)

            if error_text:
                self.state.record_error(error_text)
                logger.error("Verbinding mislukt: %s", error_text)
            else:
                logger.warning("Verbinding door de server gesloten")
            self.state.set("wachten op nieuwe poging", "error" if auth_failed else "warn", error_text)
            self.state.next_retry_at = state_now() + timedelta(seconds=wait)
            logger.info("Opnieuw verbinden over %.0f seconden", wait)
            changed = await self._wait(wait)
            delay = self.INITIAL_DELAY if changed else min(delay * 2, self.MAX_DELAY)

    async def _session(self, uri: str, token: str, lifecycle: MatchLifecycle) -> str:
        """Eén verbinding. Geeft 'changed' bij gewijzigde instellingen, 'closed' bij sluiten."""
        async with self._connect(
            uri,
            max_size=MAX_MESSAGE_BYTES,
            ping_interval=self.PING_INTERVAL,
            ping_timeout=self.PING_TIMEOUT,
            close_timeout=self.CLOSE_TIMEOUT,
        ) as websocket:
            self._connected_at = time.monotonic()
            self.state.set("verbonden", "ok")
            logger.info("Verbonden met het scorebord")
            recv_task: asyncio.Future | None = None
            change_task = asyncio.ensure_future(self.settings.connection_changed.wait())
            try:
                while True:
                    if recv_task is None:
                        recv_task = asyncio.ensure_future(websocket.recv())
                    deadline = lifecycle.next_deadline
                    timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                    done, _ = await asyncio.wait(
                        {recv_task, change_task},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        self._publish(lifecycle.flush_expired())
                        continue
                    if change_task in done:
                        return "changed"
                    task, recv_task = recv_task, None
                    try:
                        message = task.result()
                    except ConnectionClosedOK:
                        return "closed"
                    self._handle(message, token, lifecycle)
            finally:
                helpers = [task for task in (recv_task, change_task) if task is not None]
                for task in helpers:
                    task.cancel()
                # gather met return_exceptions vangt de fouten van de hulptaken op, maar laat
                # een stopsignaal voor deze taak zelf door. contextlib.suppress(CancelledError)
                # zou dat stopsignaal inslikken, waarna de lus eindeloos doorgaat.
                await asyncio.gather(*helpers, return_exceptions=True)

    def _handle(self, message: str | bytes, token: str, lifecycle: MatchLifecycle) -> None:
        text = message if isinstance(message, str) else message.decode("utf-8", "replace")
        raw_logger.info("%s", redact_token(text, token))
        self.state.last_message_at = state_now()
        self.state.messages_received += 1
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            logger.warning(
                "Could not parse websocket message for match status: %s", error
            )
            return
        try:
            events = lifecycle.update(payload)
        except Exception:  # een vreemd bericht mag de verbinding niet stoppen
            logger.exception("Fout bij het verwerken van een bericht")
            return
        self._publish(events)


def display_url(url: str) -> str:
    """Toon de URL zonder eventuele token-queryparameter."""
    try:
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "token"]
        return urlunsplit(parts._replace(query=urlencode(query)))
    except ValueError:
        return "(ongeldige URL)"


def token_text(token: str) -> str:
    """Of er een token is en wanneer het verloopt (nooit het token zelf)."""
    from datetime import datetime, timezone

    if not token:
        return "niet ingesteld"
    expiry = token_expiry(token)
    if expiry is None:
        return "ingesteld (vervaldatum onbekend)"
    days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
    when = fmt_time(expiry)
    return f"ingesteld, verlopen op {when}" if days < 0 else f"ingesteld, verloopt op {when} (over {days:.0f} dagen)"


class ScoreboardSource(Source):
    id = "scoreboard"
    label = "Scorebord"
    description = "Live via het scorebord (websocket). Je club is de thuisploeg op het scorebord."

    def __init__(
        self,
        settings: Settings,
        state: ConnectionState,
        publish: Callable[[list[str]], None],
        regular_minutes: int = 90,
        connect_func: Callable[..., Any] = connect,
    ) -> None:
        super().__init__(publish)
        self.settings = settings
        self.state = state
        self.connector = Connector(settings, state, regular_minutes, connect_func, publish=publish)

    async def run(self) -> None:
        try:
            await self.connector.run()
        finally:
            self.state.set("gestopt (andere bron gekozen)", "warn")

    def led(self) -> tuple[str, str]:
        return self.state.level, self.state.status

    def status_rows(self) -> list[StatusRow]:
        return connection_rows(self.settings, self.state)

    club_hint = "thuisploeg op het scorebord (nog geen status ontvangen)"

    def club(self) -> str | None:
        teams = self.connector.teams
        return teams[0] if teams else None

    @property
    def match(self) -> dict | None:
        teams = self.connector.teams
        return {"homeClub": teams[0], "awayClub": teams[1]} if teams else None


def connection_rows(settings: Settings, state: ConnectionState) -> list[StatusRow]:
    """Statusregels van de verbinding met het scorebord."""
    s = state
    error = s.last_error or "-"
    if s.last_error:
        error += f" ({fmt_time(s.last_error_at)})"
    return [
        ("Status", s.status, s.level),
        ("Sinds", fmt_time(s.since), None),
        ("Laatste bericht", fmt_time(s.last_message_at), None),
        ("Berichten ontvangen", str(s.messages_received), None),
        ("Verbindingspogingen", str(s.attempt), None),
        ("Volgende poging", fmt_time(s.next_retry_at), None),
        ("Laatste fout", error, None),
        ("URL", display_url(settings.url) or "-", None),
        ("Token", token_text(settings.token), None),
    ]
