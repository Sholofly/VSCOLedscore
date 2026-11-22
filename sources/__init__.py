"""Bronnen van wedstrijdinformatie en de keuze welke bron actief is.

Een bron toevoegen:
1. Maak een module in deze map met een subklasse van `Source` (zie `base.py`):
   zet `id`, `label` en `description`, en implementeer `run`, `led` en
   `status_rows`. Publiceer berichten met `self.publish([...])`.
2. Voeg hem toe in `build_sources` hieronder. Hij verschijnt dan vanzelf in de
   keuzelijst onder Connectie > Bron. Implementeer `club()` (en zo mogelijk
   `match`) zodat de pagina je club en de wedstrijd kan tonen.
3. Heeft de bron eigen instellingen, voeg dan een paneel toe in `web.py`
   (`SOURCE_PANELS`; alleen het paneel van de gekozen bron wordt getoond) en
   de instellingen in `settings.py`.
"""

import asyncio
import logging
from collections.abc import Callable

from settings import Settings
from state import ConnectionState

from .base import Publisher, Source, StatusRow
from .scoreboard import ScoreboardSource
from .voetbalnederland import VoetbalnederlandSource

logger = logging.getLogger(__name__)

__all__ = ["Publisher", "Source", "SourceManager", "StatusRow", "build_sources"]

DEFAULT_SOURCE = ScoreboardSource.id


def build_sources(
    settings: Settings,
    state: ConnectionState,
    publish: Callable[[list[str]], None],
    regular_minutes: int = 90,
) -> dict[str, Source]:
    """Alle beschikbare bronnen, in de volgorde van de keuzelijst."""
    sources: list[Source] = [
        ScoreboardSource(settings, state, publish, regular_minutes),
        VoetbalnederlandSource(settings, publish),
    ]
    return {source.id: source for source in sources}


class SourceManager:
    """Laat precies één bron draaien: de bron die onder Connectie is gekozen."""

    RESTART_DELAY = 5.0  # wachttijd voordat een vastgelopen bron opnieuw start

    def __init__(self, settings: Settings, sources: dict[str, Source]) -> None:
        if not sources:
            raise ValueError("Er is geen enkele bron.")
        self.settings = settings
        self.sources = sources

    @property
    def active_id(self) -> str:
        chosen = self.settings.source
        if chosen in self.sources:
            return chosen
        return DEFAULT_SOURCE if DEFAULT_SOURCE in self.sources else next(iter(self.sources))

    def active(self) -> Source:
        return self.sources[self.active_id]

    async def run(self) -> None:
        while True:
            self.settings.source_changed.clear()
            source = self.active()
            logger.info("Bron: %s", source.label)
            runner = asyncio.create_task(source.run())
            changed = asyncio.create_task(self.settings.source_changed.wait())
            try:
                done, _ = await asyncio.wait({runner, changed}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (runner, changed):
                    task.cancel()
                # return_exceptions vangt de fouten van de taken op, maar laat een
                # stopsignaal voor deze taak zelf door (zie CLAUDE.md, valkuilen).
                results = await asyncio.gather(runner, changed, return_exceptions=True)
            if runner in done and isinstance(results[0], Exception):
                logger.error("Bron %s is gestopt met een fout: %s", source.label, results[0])
                await asyncio.sleep(self.RESTART_DELAY)
