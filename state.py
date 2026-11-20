"""Verbindingsstatus, door de verbinding bijgewerkt en door de webomgeving getoond."""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


def now() -> datetime:
    return datetime.now().astimezone()


class MemoryLogHandler(logging.Handler):
    """Bewaart de laatste logregels in het geheugen, voor als het logbestand niet kan."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


@dataclass
class ConnectionState:
    status: str = "starten"
    level: str = "warn"  # ok | warn | error
    detail: str = ""
    since: datetime = field(default_factory=now)
    last_message_at: datetime | None = None
    messages_received: int = 0
    attempt: int = 0
    last_error: str = ""
    last_error_at: datetime | None = None
    next_retry_at: datetime | None = None
    # De laatste wedstrijdberichten (wat de app als melding heeft gegenereerd).
    # Per bericht: (tijd, tekst, foto-URL of None).
    messages: deque[tuple[datetime, str, str | None]] = field(
        default_factory=lambda: deque(maxlen=100)
    )

    def add_message(self, text: str, image: str | None = None) -> None:
        self.messages.append((now(), str(text), image))

    def set(self, status: str, level: str = "warn", detail: str = "") -> None:
        self.status = status
        self.level = level
        self.detail = detail
        self.since = now()
        if status != "wachten op nieuwe poging":
            self.next_retry_at = None

    def record_error(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = now()
