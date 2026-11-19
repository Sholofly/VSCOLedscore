"""Verbindingsstatus, door de verbinding bijgewerkt en door de webomgeving getoond."""

from dataclasses import dataclass, field
from datetime import datetime


def now() -> datetime:
    return datetime.now().astimezone()


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
