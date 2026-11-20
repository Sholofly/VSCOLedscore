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
    def __init__(self, path: Path, url: str = "", token: str = "") -> None:
        self.path = Path(path)
        self.url = url.strip()
        self.token = token.strip()
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
        templates = raw.get("templates")
        if isinstance(templates, dict):
            self.template_overrides = {
                key: value
                for key, value in templates.items()
                if key in TEMPLATE_INFO
                and isinstance(value, str)
                and validate_template(key, value) is None
            }

    def _save(self) -> None:
        """Schrijf atomair; bestand alleen leesbaar voor de eigenaar (waar mogelijk)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "url": self.url,
            "token": self.token,
            "templates": self.template_overrides,
            "goal_wait_seconds": self.goal_wait_seconds,
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
