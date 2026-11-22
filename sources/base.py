"""Gemeenschappelijke basis voor bronnen van wedstrijdinformatie.

Een bron levert wedstrijdberichten (begin, doelpunten, rust, einde ...) aan de
`Publisher`. Er draait altijd precies één bron: de bron die onder Connectie is gekozen.
Een nieuwe bron is een subklasse van `Source` die in `sources/__init__.py` wordt
geregistreerd (zie daar).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from state import ConnectionState

logger = logging.getLogger(__name__)

# Een regel in de statustabel: (label, tekst, niveau of None). Met een niveau
# (ok | warn | error) toont de webomgeving de tekst als gekleurd label.
StatusRow = tuple[str, str, str | None]


def fmt_time(moment: datetime | None) -> str:
    return "-" if moment is None else moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class Publisher:
    """Publiceert berichten van de actieve bron: log, lijst in de webomgeving en pushmeldingen."""

    def __init__(self, state: ConnectionState, notifier: Any = None) -> None:
        self.state = state
        # Krijgt elk wedstrijdbericht met notify(tekst, sleutel, soort), bv. voor pushmeldingen.
        self.notifier = notifier

    def publish(self, events: list[str]) -> None:
        for event in events:
            logger.info("%s", event)
            key = self.state.add_message(event, getattr(event, "image", None))
            if self.notifier is not None:
                try:
                    self.notifier.notify(str(event), key, getattr(event, "kind", None))
                except Exception:  # meldingen mogen een bron nooit stoppen
                    logger.exception("Pushmelding kon niet worden ingepland")


class Source:
    """Basis voor een bron. Subklassen zetten `id`, `label` en `description` en
    implementeren `run`, `led` en `status_rows`."""

    id = ""
    label = ""
    description = ""

    def __init__(self, publish: Callable[[list[str]], None]) -> None:
        self.publish = publish

    async def run(self) -> None:
        """Draait zolang deze bron de actieve bron is; wordt geannuleerd bij een wissel."""
        raise NotImplementedError

    def led(self) -> tuple[str, str]:
        """(niveau, tekst) voor het statuslampje."""
        return "warn", "starten"

    def status_rows(self) -> list[StatusRow]:
        """Regels voor de statustabel onder Connectie > Status."""
        return []

    def club(self) -> str | None:
        """Naam van je club bij deze bron, of None als die (nog) niet bekend is."""
        return None

    # Uitleg als club() nog niets weet, bv. "kies een team".
    club_hint = "nog onbekend"

    @property
    def match(self) -> dict | None:
        """De wedstrijd van vandaag als die bekend is ({homeClub, awayClub, ...}), anders None."""
        return None
