"""Web Push: sleutels (VAPID), aangemelde apparaten en het versturen van meldingen.

Een melding gaat via de pushdienst van de telefoon (Apple, Google, Mozilla,
Microsoft), versleuteld; die diensten kunnen de inhoud niet lezen. Wie de
startpagina van de app open en zichtbaar heeft, krijgt geen melding: die ziet
het bericht daar al.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush_async

logger = logging.getLogger(__name__)

# Alleen de echte pushdiensten; zo kan een aanmelding niet worden misbruikt om de
# server naar willekeurige adressen te laten verbinden.
ALLOWED_PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.apple.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)
MAX_SUBSCRIPTIONS = 20
PRESENT_SECONDS = 6.0  # zo lang na de laatste update geldt de startpagina als "open"
TTL_SECONDS = 600  # een melding die na 10 minuten nog niet is afgeleverd, vervalt
TITLE = "Matchshare"
# Icoon per soort bericht: een bal bij doelpunten, anders een fluitje. Het icoon is
# gekleurd; de badge (statusbalk van Android) is een witte vorm op transparant, want
# Android gebruikt alleen de vorm.
GOAL_KINDS = {"goal_home", "goal_home_no_scorer", "goal_away"}


def notification_icons(kind: str | None) -> tuple[str, str]:
    """(icoon, badge) voor een soort bericht; zonder soort (testmelding) de bal."""
    name = "ball" if kind is None or kind in GOAL_KINDS else "whistle"
    return f"/static/icon-{name}.png", f"/static/badge-{name}.png"


# Contactadres voor de pushdiensten: "mailto:..." of "https://domein" (zonder pad).
DEFAULT_SUBJECT = "https://github.com"
_B64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


def load_vapid(path: Path) -> Vapid02:
    """Sleutelpaar voor Web Push; één keer aangemaakt en bewaard (net als de sessiesleutel).

    Gaat het verloren, dan moet elk apparaat zich opnieuw aanmelden.
    """
    try:
        return Vapid02.from_pem(path.read_bytes())
    except (OSError, ValueError, TypeError):
        pass
    vapid = Vapid02()
    vapid.generate_keys()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(vapid.private_pem())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as error:
        logger.warning("Pushsleutel niet op te slaan (%s); na een herstart opnieuw aanmelden", error)
    return vapid


def public_key(vapid: Vapid02) -> str:
    """De publieke sleutel zoals de browser hem verwacht (applicationServerKey)."""
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def validate_subject(vapid: Vapid02, subject: str) -> str:
    """Pushdiensten weigeren een ongeldig contactadres; controleer het bij het starten."""
    try:
        vapid.sign({"sub": subject, "aud": "https://fcm.googleapis.com"})
    except Exception:
        raise ValueError(
            "VAPID_SUBJECT must be 'mailto:name@domain' or 'https://domain' (without a path)."
        ) from None
    return subject


def endpoint_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def validate_subscription(data: Any) -> dict:
    """Controleer een aanmelding uit de browser; geeft een opgeschoonde versie terug."""
    if not isinstance(data, dict):
        raise ValueError("Ongeldige aanmelding.")
    endpoint = data.get("endpoint")
    keys = data.get("keys")
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or not isinstance(keys, dict):
        raise ValueError("Ongeldige aanmelding.")
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(
        host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_PUSH_HOSTS
    ):
        raise ValueError("Onbekende pushdienst.")
    clean_keys = {}
    for name, limit in (("p256dh", 200), ("auth", 50)):
        value = keys.get(name)
        if not isinstance(value, str) or not value or len(value) > limit or not _B64URL.match(value):
            raise ValueError("Ongeldige aanmelding.")
        clean_keys[name] = value
    return {"endpoint": endpoint, "keys": clean_keys}


class PushStore:
    """Aangemelde apparaten, bewaard in het datavolume."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.subscriptions: list[dict] = []
        self._present: dict[str, float] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            logger.error("Bestand met aanmeldingen onleesbaar; opnieuw beginnen: %s", error)
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                self.subscriptions.append(validate_subscription(item))
            except ValueError:
                continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(self.subscriptions, file, indent=2)
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

    def find(self, endpoint: str) -> dict | None:
        return next((s for s in self.subscriptions if s["endpoint"] == endpoint), None)

    def add(self, subscription: dict) -> None:
        subscription = validate_subscription(subscription)
        self.subscriptions = [s for s in self.subscriptions if s["endpoint"] != subscription["endpoint"]]
        self.subscriptions.append(subscription)
        del self.subscriptions[:-MAX_SUBSCRIPTIONS]  # oudste eruit als het er te veel worden
        self._save()

    def remove(self, endpoint: str) -> bool:
        before = len(self.subscriptions)
        self.subscriptions = [s for s in self.subscriptions if s["endpoint"] != endpoint]
        if len(self.subscriptions) != before:
            self._save()
            return True
        return False

    def mark_present(self, endpoint: str, now: float | None = None) -> None:
        """Dit apparaat heeft de startpagina open en zichtbaar."""
        if self.find(endpoint) is not None:
            self._present[endpoint] = time.monotonic() if now is None else now

    def is_present(self, endpoint: str, now: float | None = None) -> bool:
        seen = self._present.get(endpoint)
        now = time.monotonic() if now is None else now
        return seen is not None and now - seen <= PRESENT_SECONDS


SendFunc = Callable[..., Awaitable[Any]]


def _status(error: WebPushException) -> int | None:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) or getattr(response, "status", None)


class PushSender:
    def __init__(
        self, store: PushStore, vapid: Vapid02, subject: str, send_func: SendFunc = webpush_async
    ) -> None:
        self.store = store
        self.vapid = vapid
        self.subject = subject
        self._send = send_func
        self._tasks: set[asyncio.Task] = set()

    @property
    def public_key(self) -> str:
        return public_key(self.vapid)

    async def send_one(
        self, subscription: dict, text: str, key: str | None, kind: str | None = None
    ) -> str:
        """Verstuur één melding: "sent", "gone" (apparaat afgemeld, opgeruimd) of "failed"."""
        # Een melding met hetzelfde label vervangt de vorige stil (geen nieuwe pop-up);
        # daarom krijgt elke testmelding een eigen label.
        tag = key or f"test-{time.time_ns()}"
        icon, badge = notification_icons(kind)
        payload = json.dumps(
            {
                "title": TITLE, "body": text, "url": f"/share/{key}" if key else "/", "tag": tag,
                "icon": icon, "badge": badge,
            },
            ensure_ascii=False,
        )
        try:
            await self._send(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=self.vapid,
                vapid_claims={"sub": self.subject},  # nieuwe dict: pywebpush vult "aud" en "exp" aan
                ttl=TTL_SECONDS,
                timeout=10,
                headers={"Urgency": "high"},
            )
            return "sent"
        except WebPushException as error:
            status = _status(error)
            if status in (404, 410):
                self.store.remove(subscription["endpoint"])
                logger.info("Apparaat %s is afgemeld voor meldingen", endpoint_id(subscription["endpoint"]))
                return "gone"
            logger.warning(
                "Melding niet afgeleverd bij %s (HTTP %s)", endpoint_id(subscription["endpoint"]), status
            )
        except Exception as error:  # netwerkfout e.d.: niet de app laten stoppen
            logger.warning(
                "Melding niet afgeleverd bij %s: %s", endpoint_id(subscription["endpoint"]), error
            )
        return "failed"

    async def send(self, text: str, key: str, kind: str | None = None) -> int:
        """Stuur een wedstrijdbericht naar alle apparaten, behalve wie de startpagina open heeft."""
        targets = [s for s in list(self.store.subscriptions) if not self.store.is_present(s["endpoint"])]
        results = await asyncio.gather(*(self.send_one(s, text, key, kind) for s in targets))
        return sum(1 for result in results if result == "sent")

    def notify(self, text: str, key: str, kind: str | None = None) -> None:
        """Verstuur op de achtergrond; de verbinding met het scorebord wacht er niet op."""
        if not self.store.subscriptions:
            return
        task = asyncio.get_running_loop().create_task(self.send(text, key, kind))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
