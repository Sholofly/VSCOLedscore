import asyncio
import contextlib
import json
import logging
import math
import os
import random
import re
import signal
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from dotenv import dotenv_values
from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosedOK,
    InvalidStatus,
    WebSocketException,
)

from messages import DEFAULT_TEMPLATES, render_message
from settings import Settings
from state import ConnectionState, MemoryLogHandler, now as state_now

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logger = logging.getLogger(__name__)
raw_logger = logging.getLogger(f"{__name__}.raw")
raw_logger.propagate = False  # ruwe berichten nooit naar de console

GOAL_WAIT_SECONDS = 20.0  # zo lang wachten op de doelpuntmaker
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def redact_token(message: str, token: str) -> str:
    if not token:
        return message
    return message.replace(token, "[redacted]").replace(
        quote(token, safe=""), "[redacted]"
    )


class TokenRedactingFilter(logging.Filter):
    """Haal het huidige token uit elke logregel en uit tracebacks."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    def filter(self, record: logging.LogRecord) -> bool:
        token = self.settings.token
        if not token:
            return True
        message = record.getMessage()
        redacted = redact_token(message, token)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact_token(text, token)
        return True


def setup_raw_log(path: str) -> bool:
    """Schrijf alle ruwe websocketberichten naar een roterend logbestand.

    Geeft False terug (zonder te crashen) als het bestand niet schrijfbaar is.
    """
    log_path = Path(path)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError as error:
        logger.warning("Ruwe berichten worden niet bewaard: %s niet schrijfbaar (%s)", log_path, error)
        return False
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    raw_logger.setLevel(logging.INFO)
    raw_logger.addHandler(handler)
    return True


def setup_logging(settings: Settings, app_log: Path) -> MemoryLogHandler:
    """Log naar console, een roterend bestand en het geheugen (voor de webomgeving)."""
    formatter = logging.Formatter(LOG_FORMAT)
    memory = MemoryLogHandler()
    handlers: list[logging.Handler] = [logging.StreamHandler(), memory]
    file_error: OSError | None = None
    try:
        app_log.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(app_log, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        )
    except OSError as error:  # bv. logmap niet schrijfbaar op een NAS
        file_error = error
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(TokenRedactingFilter(settings))
        root.addHandler(handler)
    # Bibliotheken kunnen de URL (met token) loggen; alleen waarschuwingen tonen.
    for name in ("websockets", "aiohttp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if file_error is not None:
        logger.warning(
            "Logbestand %s niet schrijfbaar (%s); alleen logging naar de console. "
            "Geef de map schrijfrechten voor gebruiker-id 1000.",
            app_log,
            file_error,
        )
    return memory


class MatchLifecycle:
    def __init__(
        self,
        regular_minutes: int = 90,
        templates: Mapping[str, str] | Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self.regular_minutes = regular_minutes
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
        self.scorers: set[tuple[str, int, str]] = set()
        # Eigen doelpunt dat wacht op een doelpuntmaker: (context, deadline)
        self.pending_goal: tuple[dict[str, Any], float] | None = None

    @property
    def next_deadline(self) -> float | None:
        return self.pending_goal[1] if self.pending_goal else None

    def _render(self, key: str, context: dict[str, Any]) -> list[str]:
        try:
            text = render_message(self._templates(), key, context)
        except Exception:  # een kapot sjabloon mag de verwerking niet stoppen
            logger.exception("Sjabloon '%s' kon niet worden ingevuld", key)
            text = render_message(DEFAULT_TEMPLATES, key, context)
        return [text] if text else []

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
        scoreboard = data.get("scoreboard")
        scoreboard = scoreboard if isinstance(scoreboard, dict) else {}
        score = (scoreboard.get("home"), scoreboard.get("away"))
        minute = self._minute(data)
        context = self._context(home, away, score, "?" if minute is None else str(minute))
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
            if is_active and not started and scored_side == "away":
                events.extend(self._render("goal_away", context))
            elif is_active and not started and scored_side == "home":
                events.extend(self.flush_pending())
                self.pending_goal = (context, now + GOAL_WAIT_SECONDS)
            # Alleen de doelpuntmaker van de eigen (thuis)ploeg wordt gebruikt.
            for side, _, name in sorted(scorers - self.scorers):
                if side == "home" and self.pending_goal:
                    goal_context, _ = self.pending_goal
                    self.pending_goal = None
                    events.extend(self._render("goal_home", {**goal_context, "speler": name}))

        self.started_at = started_at
        self.ended_at = ended_at
        self.active = is_active
        self.rest = is_rest
        self.score = score
        self.scorers = scorers
        self.initialized = True
        return events

    @staticmethod
    def _context(
        home: str, away: str, score: tuple[Any, Any], minute: str
    ) -> dict[str, Any]:
        tag = re.sub(r"[^A-Za-z]", "", away)[:3].lower()
        home_score = "?" if score[0] is None else score[0]
        away_score = "?" if score[1] is None else score[1]
        return {
            "minuut": minute,
            "thuis": home,
            "uit": away,
            "tegenstander": away,
            "stand": f"{home_score}-{away_score}",
            "thuis_score": home_score,
            "uit_score": away_score,
            "hashtag": f"#vsc{tag}",
            "blessuretijd": 0,
        }

    def _end_messages(
        self, seconds: Any, score: tuple[Any, Any], context: dict[str, Any]
    ) -> list[str]:
        extra = 0
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            minute = "?"
        else:
            played = math.ceil(seconds / 60)  # 90:00 is 90, 92:01 is 90 + 3
            extra = max(0, played - self.regular_minutes)
            minute = str(min(played, self.regular_minutes))
            if extra:
                minute += f" + {extra}"
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

    @staticmethod
    def _scorers(data: dict[str, Any]) -> set[tuple[str, int, str]]:
        """Doelpuntmakers als (kant, positie, naam); de lijst kan null bevatten."""
        raw = data.get("scorers")
        result: set[tuple[str, int, str]] = set()
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
                    result.add((side, index, str(name)))
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

    def __init__(
        self,
        settings: Settings,
        state: ConnectionState,
        regular_minutes: int = 90,
        connect_func: Callable[..., Any] = connect,
    ) -> None:
        self.settings = settings
        self.state = state
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
                self.regular_minutes, lambda: self.settings.templates
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

    def _publish(self, events: list[str]) -> None:
        for event in events:
            logger.info("%s", event)
            self.state.add_message(event)

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
        async with self._connect(uri, max_size=MAX_MESSAGE_BYTES) as websocket:
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


def read_setting(config: Mapping[str, str | None], name: str, default: str = "") -> str:
    return os.getenv(name) or config.get(name) or default


async def main() -> None:
    from web import start_web

    config = dotenv_values()
    data_dir = Path(read_setting(config, "DATA_DIR", "data"))
    log_dir = Path(read_setting(config, "LOG_DIR", "logs"))
    settings = Settings(
        data_dir / "settings.json",
        read_setting(config, "WS_URL"),
        read_setting(config, "WS_TOKEN"),
    )
    memory_log = setup_logging(settings, log_dir / "app.log")
    setup_raw_log(read_setting(config, "RAW_LOG_FILE", str(log_dir / "websocket-raw.log")))

    minutes = read_setting(config, "REGULAR_MINUTES", "90")
    if not minutes.isdigit() or int(minutes) < 1:
        raise ValueError("REGULAR_MINUTES must be a positive whole number.")
    port = read_setting(config, "WEB_PORT", "8080")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("WEB_PORT must be a port number between 1 and 65535.")
    password = read_setting(config, "WEB_PASSWORD")
    if not password:
        # Zonder webomgeving kan niets de configuratie herstellen: direct controleren.
        build_websocket_uri(settings.url, settings.token)
        logger.warning("WEB_PASSWORD is niet ingesteld: de webomgeving staat uit")

    state = ConnectionState()
    connector = Connector(settings, state, int(minutes))
    web_runner = None
    if password:
        web_runner = await start_web(
            settings,
            state,
            app_log=log_dir / "app.log",
            raw_log=Path(read_setting(config, "RAW_LOG_FILE", str(log_dir / "websocket-raw.log"))),
            memory_log=memory_log,
            username=read_setting(config, "WEB_USER", "admin"),
            password=password,
            host=read_setting(config, "WEB_HOST", "127.0.0.1"),
            port=int(port),
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # o.a. Windows
            loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(connector.run())
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        runner.cancel()
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        if web_runner is not None:
            await web_runner.cleanup()
        logger.info("Gestopt")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except ValueError as error:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        logger.error("%s", error)
        raise SystemExit(1)
