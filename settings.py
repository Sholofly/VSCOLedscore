"""Wijzigbare instellingen (URL, token, sjablonen), bewaard in data/settings.json.

Waarden uit .env zijn de beginwaarden; wat via de webomgeving is opgeslagen
heeft voorrang. Het token wordt nooit teruggegeven aan de webomgeving.
"""

import asyncio
import base64
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from messages import DEFAULT_TEMPLATES, TEMPLATE_INFO, validate_template

logger = logging.getLogger(__name__)

MAX_TOKEN_LENGTH = 4096
DEFAULT_GOAL_WAIT_SECONDS = 20
MAX_GOAL_WAIT_SECONDS = 300
MAX_URL_LENGTH = 2048
# Uitwedstrijden via tikkieterug
DEFAULT_TIKKIE_URL = "https://tikkieterug.sholofly.nl"
SOURCE_MODES = {
    "auto": "Automatisch: thuis het scorebord, uit tikkieterug",
    "scoreboard": "Altijd het scorebord",
    "tikkieterug": "Altijd tikkieterug",
}
DEFAULT_POLL_SECONDS = 30
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 300
MAX_NAME_LENGTH = 60


def validate_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if (
        len(url) > MAX_URL_LENGTH
        or any(ord(char) < 33 for char in url)
        or parts.scheme not in {"ws", "wss"}
        or not parts.netloc
    ):
        raise ValueError("De URL moet beginnen met ws:// of wss:// en mag geen spaties bevatten.")
    if any(key == "token" for key, _ in parse_qsl(parts.query)):
        raise ValueError("Zet het token niet in de URL; gebruik het tokenveld.")
    return url


def validate_goal_wait(value: object) -> int:
    """Wachttijd op de doelpuntmaker in hele seconden (0 = niet wachten)."""
    try:
        seconds = int(str(value).strip())
    except ValueError:
        raise ValueError("Vul een heel aantal seconden in.") from None
    if not 0 <= seconds <= MAX_GOAL_WAIT_SECONDS:
        raise ValueError(f"Kies een wachttijd van 0 tot {MAX_GOAL_WAIT_SECONDS} seconden.")
    return seconds


def validate_tikkie_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if len(url) > MAX_URL_LENGTH or any(ord(c) < 33 for c in url) or parts.scheme != "https" or not parts.netloc:
        raise ValueError("Het adres van tikkieterug moet met https:// beginnen en mag geen spaties bevatten.")
    return url


def validate_poll_seconds(value: object) -> int:
    try:
        seconds = int(str(value).strip())
    except ValueError:
        raise ValueError("Vul een heel aantal seconden in.") from None
    if not MIN_POLL_SECONDS <= seconds <= MAX_POLL_SECONDS:
        raise ValueError(f"Kies {MIN_POLL_SECONDS} tot {MAX_POLL_SECONDS} seconden.")
    return seconds


def validate_name(name: str) -> str:
    name = " ".join(str(name).split())
    if len(name) > MAX_NAME_LENGTH or any(ord(c) < 32 for c in name):
        raise ValueError(f"Maximaal {MAX_NAME_LENGTH} tekens.")
    return name


def validate_token(token: str) -> str:
    token = token.strip()
    if not token:
        raise ValueError("Het token mag niet leeg zijn.")
    if len(token) > MAX_TOKEN_LENGTH or any(ord(char) < 32 for char in token):
        raise ValueError("Het token bevat ongeldige tekens of is te lang.")
    return token


def token_expiry(token: str) -> datetime | None:
    """Lees alleen de vervaldatum (`exp`) uit een JWT; geef nooit andere claims terug."""
    try:
        parts = token.removeprefix("JWT").strip().split(".")
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(float(claims["exp"]), tz=timezone.utc)
    except (IndexError, KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


class Settings:
    def __init__(
        self,
        path: Path,
        url: str = "",
        token: str = "",
        tikkie_url: str = DEFAULT_TIKKIE_URL,
    ) -> None:
        self.path = Path(path)
        self.url = url.strip()
        self.token = token.strip()
        # Uitwedstrijden (tikkieterug, openbare API zonder sleutel).
        self.tikkie_url = (tikkie_url or DEFAULT_TIKKIE_URL).strip()
        self.team_id: int | None = None
        self.team_name = ""
        self.club_name = ""  # eigen clubnaam in berichten bij uitduels (leeg = naam uit tikkieterug)
        self.source_mode = "auto"
        self.poll_seconds = DEFAULT_POLL_SECONDS
        # Wordt gezet als iets van tikkieterug verandert, zodat de watcher opnieuw kijkt.
        self.tikkie_changed = asyncio.Event()
        self.template_overrides: dict[str, str] = {}
        self.goal_wait_seconds: float = DEFAULT_GOAL_WAIT_SECONDS
        # Wordt gezet als URL of token verandert, zodat de verbinding opnieuw opbouwt.
        self.connection_changed = asyncio.Event()
        self._load()

    @property
    def templates(self) -> dict[str, str]:
        return {**DEFAULT_TEMPLATES, **self.template_overrides}

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            logger.error("Instellingenbestand onleesbaar, standaardwaarden gebruikt: %s", error)
            return
        if not isinstance(raw, dict):
            logger.error("Instellingenbestand heeft een onverwacht formaat; genegeerd.")
            return
        if isinstance(raw.get("url"), str) and raw["url"].strip():
            self.url = raw["url"].strip()
        if isinstance(raw.get("token"), str) and raw["token"].strip():
            self.token = raw["token"].strip()
        if "goal_wait_seconds" in raw and not isinstance(raw["goal_wait_seconds"], bool):
            try:
                self.goal_wait_seconds = validate_goal_wait(raw["goal_wait_seconds"])
            except ValueError:
                logger.error("Ongeldige wachttijd in het instellingenbestand; standaard gebruikt.")
        self._load_tikkie(raw)
        templates = raw.get("templates")
        if isinstance(templates, dict):
            self.template_overrides = {
                key: value
                for key, value in templates.items()
                if key in TEMPLATE_INFO
                and isinstance(value, str)
                and validate_template(key, value) is None
            }

    def _load_tikkie(self, raw: dict) -> None:
        def take(name, check):
            if name in raw and raw[name] not in (None, ""):
                try:
                    return check(raw[name])
                except (ValueError, TypeError):
                    logger.error("Ongeldige waarde voor %s in het instellingenbestand; genegeerd.", name)
            return None

        self.tikkie_url = take("tikkie_url", lambda v: validate_tikkie_url(str(v))) or self.tikkie_url
        team_id = raw.get("team_id")
        if isinstance(team_id, int) and not isinstance(team_id, bool) and team_id > 0:
            self.team_id = team_id
        self.team_name = take("team_name", validate_name) or ""
        self.club_name = take("club_name", validate_name) or ""
        if raw.get("source_mode") in SOURCE_MODES:
            self.source_mode = raw["source_mode"]
        poll = take("poll_seconds", validate_poll_seconds)
        if poll is not None:
            self.poll_seconds = poll

    def _save(self) -> None:
        """Schrijf atomair; bestand alleen leesbaar voor de eigenaar (waar mogelijk)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "url": self.url,
            "token": self.token,
            "templates": self.template_overrides,
            "goal_wait_seconds": self.goal_wait_seconds,
            "tikkie_url": self.tikkie_url,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "club_name": self.club_name,
            "source_mode": self.source_mode,
            "poll_seconds": self.poll_seconds,
        }
        handle, temp_name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def set_url(self, url: str) -> None:
        url = validate_url(url)
        if url != self.url:
            self.url = url
            self._save()
            self.connection_changed.set()

    def set_token(self, token: str) -> None:
        token = validate_token(token)
        if token != self.token:
            self.token = token
            self._save()
            self.connection_changed.set()

    @property
    def tikkie_ready(self) -> bool:
        """Kan tikkieterug gebruikt worden (team gekozen)?"""
        return bool(self.team_id)

    def _tikkie_update(self, **values) -> None:
        if any(getattr(self, name) != value for name, value in values.items()):
            for name, value in values.items():
                setattr(self, name, value)
            self._save()
            self.tikkie_changed.set()

    def set_tikkie_url(self, url: str) -> None:
        self._tikkie_update(tikkie_url=validate_tikkie_url(url))

    def set_team(self, team_id: object, name: str) -> None:
        try:
            number = int(str(team_id).strip())
        except ValueError:
            raise ValueError("Ongeldig team.") from None
        if number <= 0:
            raise ValueError("Ongeldig team.")
        self._tikkie_update(team_id=number, team_name=validate_name(name))

    def set_club_name(self, name: str) -> None:
        self._tikkie_update(club_name=validate_name(name))

    def set_source_mode(self, mode: str) -> None:
        if mode not in SOURCE_MODES:
            raise ValueError("Onbekende keuze voor de bron.")
        self._tikkie_update(source_mode=mode)

    def set_poll_seconds(self, value: object) -> None:
        self._tikkie_update(poll_seconds=validate_poll_seconds(value))

    def set_goal_wait(self, value: object) -> None:
        """Geldt vanaf het volgende doelpunt; de verbinding hoeft niet opnieuw op."""
        seconds = validate_goal_wait(value)
        if seconds != self.goal_wait_seconds:
            self.goal_wait_seconds = seconds
            self._save()

    def set_templates(self, templates: dict[str, str]) -> None:
        """Sla sjablonen op; alleen afwijkingen van de standaard worden bewaard."""
        errors = {
            key: error
            for key in TEMPLATE_INFO
            if key in templates and (error := validate_template(key, templates[key]))
        }
        if errors:
            raise ValueError("; ".join(f"{key}: {error}" for key, error in errors.items()))
        merged = {**self.templates, **{k: v for k, v in templates.items() if k in TEMPLATE_INFO}}
        self.template_overrides = {
            key: value for key, value in merged.items() if value != DEFAULT_TEMPLATES[key]
        }
        self._save()

    def reset_templates(self) -> None:
        self.template_overrides = {}
        self._save()
