"""MatchShareNL: wedstrijdinformatie delen.

Start de webomgeving en de gekozen bron (zie `sources/`): het scorebord, Voetbalnederland
of een andere geregistreerde bron. De berichten van de bron komen in de webomgeving,
in de logging en als pushmelding.
"""

import asyncio
import contextlib
import logging
import os
import signal
import traceback
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import dotenv_values

from settings import Settings
from sources.scoreboard import build_websocket_uri, raw_logger, redact_token
from state import ConnectionState, MemoryLogHandler

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logger = logging.getLogger(__name__)


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


def read_setting(config: Mapping[str, str | None], name: str, default: str = "") -> str:
    return os.getenv(name) or config.get(name) or default


async def main() -> None:
    from push import DEFAULT_SUBJECT, PushSender, PushStore, load_vapid, validate_subject
    from settings import DEFAULT_VOETBALNEDERLAND_URL
    from sources import Publisher, SourceManager, build_sources
    from web import load_session_secret, start_web

    config = dotenv_values()
    data_dir = Path(read_setting(config, "DATA_DIR", "data"))
    log_dir = Path(read_setting(config, "LOG_DIR", "logs"))
    settings = Settings(
        data_dir / "settings.json",
        read_setting(config, "WS_URL"),
        read_setting(config, "WS_TOKEN"),
        voetbalnederland_url=read_setting(config, "VOETBALNEDERLAND_URL", DEFAULT_VOETBALNEDERLAND_URL),
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
    session_days = read_setting(config, "SESSION_DAYS", "30")
    if not session_days.isdigit() or not 1 <= int(session_days) <= 365:
        raise ValueError("SESSION_DAYS must be a whole number between 1 and 365.")
    if not password:
        # Zonder webomgeving kan niets de configuratie herstellen: direct controleren.
        if settings.source == "scoreboard":
            build_websocket_uri(settings.url, settings.token)
        logger.warning("WEB_PASSWORD is niet ingesteld: de webomgeving staat uit")

    state = ConnectionState()
    pusher = None
    if password:  # aanmelden voor meldingen gaat via de webomgeving
        vapid = load_vapid(data_dir / "vapid_private.pem")
        pusher = PushSender(
            PushStore(data_dir / "push_subscriptions.json"),
            vapid,
            # Pushdiensten eisen een contactadres: mailto:... of https://domein (zonder pad).
            subject=validate_subject(vapid, read_setting(config, "VAPID_SUBJECT", DEFAULT_SUBJECT)),
        )
    publisher = Publisher(state, notifier=pusher)
    manager = SourceManager(settings, build_sources(settings, state, publisher.publish, int(minutes)))
    web_runner = None
    if password:
        web_runner = await start_web(
            settings,
            state,
            app_log=log_dir / "app.log",
            raw_log=Path(read_setting(config, "RAW_LOG_FILE", str(log_dir / "websocket-raw.log"))),
            memory_log=memory_log,
            session_secret=load_session_secret(data_dir / "session_secret"),
            push=pusher,
            sources=manager,
            # Tweede login: alleen berichten delen en eigen instellingen (leeg = uit).
            share_user=read_setting(config, "SHARE_USER", "gebruiker"),
            share_password=read_setting(config, "SHARE_PASSWORD"),
            session_days=int(session_days),
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

    runner = asyncio.create_task(manager.run())
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (runner, stopper):
            task.cancel()
        await asyncio.gather(runner, return_exceptions=True)
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
