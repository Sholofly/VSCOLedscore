import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import BasicAuth
from aiohttp.test_utils import AioHTTPTestCase
from pywebpush import WebPushException

from app import Connector
from push import (
    MAX_SUBSCRIPTIONS,
    PRESENT_SECONDS,
    PushSender,
    PushStore,
    load_vapid,
    validate_subscription,
)
from settings import Settings
from state import ConnectionState
from web import create_web_app

AUTH = {"Authorization": BasicAuth("admin", "wachtwoord").encode()}
KEYS = {"p256dh": "BPx" + "a" * 84, "auth": "abcdefghijklmnopqrstuv"}


def sub(n=1, host="fcm.googleapis.com"):
    return {"endpoint": f"https://{host}/fcm/send/apparaat-{n}", "keys": dict(KEYS)}


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakePush:
    """Vervangt webpush_async: onthoudt wat er verstuurd zou worden."""

    def __init__(self):
        self.sent = []
        self.fail = {}  # endpoint -> statuscode of Exception

    async def __call__(self, subscription_info, data, **kwargs):
        endpoint = subscription_info["endpoint"]
        outcome = self.fail.get(endpoint)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome:
            raise WebPushException("mislukt", response=FakeResponse(outcome))
        self.sent.append((endpoint, json.loads(data), kwargs))


class SubscriptionTests(unittest.TestCase):
    def test_only_real_push_services_over_https_are_accepted(self):
        for host in ("fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com",
                     "wns2-par02p.notify.windows.com"):
            self.assertEqual(validate_subscription(sub(host=host))["endpoint"], sub(host=host)["endpoint"])
        bad = [
            {"endpoint": "http://fcm.googleapis.com/x", "keys": KEYS},
            sub(host="evil.test"),
            sub(host="fcm.googleapis.com.evil.test"),
            sub(host="notfcm.googleapis.com"),
            {"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": "a b", "auth": "x"}},
            {"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": KEYS["p256dh"]}},
            {"endpoint": "https://fcm.googleapis.com/" + "x" * 3000, "keys": KEYS},
            "geen dict",
            None,
        ]
        for data in bad:
            with self.assertRaises(ValueError, msg=str(data)[:60]):
                validate_subscription(data)


class PushStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "data" / "push.json"

    def test_add_replace_remove_and_persist(self):
        store = PushStore(self.path)
        store.add(sub(1))
        store.add(sub(2))
        store.add(sub(1))  # opnieuw aanmelden vervangt, geen dubbele
        self.assertEqual([s["endpoint"] for s in PushStore(self.path).subscriptions],
                         [sub(2)["endpoint"], sub(1)["endpoint"]])
        self.assertTrue(store.remove(sub(2)["endpoint"]))
        self.assertFalse(store.remove(sub(2)["endpoint"]))
        self.assertEqual(len(PushStore(self.path).subscriptions), 1)

    def test_number_of_devices_is_limited_oldest_go_first(self):
        store = PushStore(self.path)
        for n in range(MAX_SUBSCRIPTIONS + 3):
            store.add(sub(n))
        endpoints = [s["endpoint"] for s in store.subscriptions]
        self.assertEqual(len(endpoints), MAX_SUBSCRIPTIONS)
        self.assertNotIn(sub(0)["endpoint"], endpoints)
        self.assertIn(sub(MAX_SUBSCRIPTIONS + 2)["endpoint"], endpoints)

    def test_corrupt_or_tampered_file_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{kapot", encoding="utf-8")
        with self.assertLogs("push", level="ERROR"):
            self.assertEqual(PushStore(self.path).subscriptions, [])
        self.path.write_text(json.dumps([sub(1), sub(host="evil.test")]), encoding="utf-8")
        self.assertEqual(len(PushStore(self.path).subscriptions), 1)

    def test_presence_only_for_known_devices_and_only_briefly(self):
        store = PushStore(self.path)
        store.add(sub(1))
        store.mark_present(sub(1)["endpoint"], now=100)
        store.mark_present("https://fcm.googleapis.com/onbekend", now=100)
        self.assertTrue(store.is_present(sub(1)["endpoint"], now=100 + PRESENT_SECONDS))
        self.assertFalse(store.is_present(sub(1)["endpoint"], now=100 + PRESENT_SECONDS + 0.1))
        self.assertFalse(store.is_present("https://fcm.googleapis.com/onbekend", now=100))


class PushSenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.store = PushStore(root / "push.json")
        self.fake = FakePush()
        self.sender = PushSender(self.store, load_vapid(root / "vapid.pem"), "https://example.test", self.fake)

    async def test_message_goes_to_every_device_and_opens_the_share_screen(self):
        self.store.add(sub(1))
        self.store.add(sub(2))
        self.assertEqual(await self.sender.send("'12 DOELPUNT VSCO'61!", "abc123def456"), 2)
        self.assertEqual(len(self.fake.sent), 2)
        _, payload, kwargs = self.fake.sent[0]
        self.assertEqual(payload["body"], "'12 DOELPUNT VSCO'61!")
        self.assertEqual(payload["url"], "/share/abc123def456")
        self.assertEqual(payload["tag"], "abc123def456")
        self.assertEqual(kwargs["vapid_claims"], {"sub": "https://example.test"})
        self.assertEqual(kwargs["headers"], {"Urgency": "high"})
        self.assertGreater(kwargs["ttl"], 0)

    async def test_no_notification_for_a_device_with_the_home_page_open(self):
        self.store.add(sub(1))
        self.store.add(sub(2))
        self.store.mark_present(sub(1)["endpoint"])
        await self.sender.send("bericht", "abc123def456")
        self.assertEqual([e for e, _, _ in self.fake.sent], [sub(2)["endpoint"]])

    async def test_gone_devices_are_removed_other_errors_keep_the_device(self):
        for n in (1, 2, 3, 4):
            self.store.add(sub(n))
        self.fake.fail = {sub(1)["endpoint"]: 410, sub(2)["endpoint"]: 404,
                          sub(3)["endpoint"]: 500, sub(4)["endpoint"]: OSError("geen netwerk")}
        with self.assertLogs("push", level="INFO"):
            await self.sender.send("bericht", "abc123def456")
        self.assertEqual([s["endpoint"] for s in self.store.subscriptions],
                         [sub(3)["endpoint"], sub(4)["endpoint"]])

    async def test_notify_runs_in_the_background_and_skips_when_nobody_listens(self):
        self.sender.notify("bericht", "abc123def456")  # niemand aangemeld: niets
        self.assertEqual(self.sender._tasks, set())
        self.store.add(sub(1))
        self.sender.notify("bericht", "abc123def456")
        await asyncio.gather(*self.sender._tasks)
        self.assertEqual(len(self.fake.sent), 1)


class RealEncryptionTests(unittest.IsolatedAsyncioTestCase):
    """Zonder nep: echte versleuteling en VAPID, naar een lokale 'pushdienst', ontsleuteld zoals een telefoon."""

    async def test_message_arrives_encrypted_signed_and_readable_for_the_device(self):
        import base64
        import os

        import http_ece
        from aiohttp import web as aioweb
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from py_vapid import Vapid02

        from push import DEFAULT_SUBJECT

        def b64(data):
            return base64.urlsafe_b64encode(data).decode().rstrip("=")

        device_key = ec.generate_private_key(ec.SECP256R1())
        device_pub = device_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        auth_secret = os.urandom(16)
        received = {}

        async def push_service(request):
            received["headers"] = {k.lower(): v for k, v in request.headers.items()}
            received["body"] = await request.read()
            return aioweb.Response(status=201)

        app = aioweb.Application()
        app.add_routes([aioweb.post("/push/{id}", push_service)])
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = site._server.sockets[0].getsockname()[1]

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        sender = PushSender(PushStore(root / "p.json"), load_vapid(root / "v.pem"), DEFAULT_SUBJECT)
        device = {
            "endpoint": f"http://127.0.0.1:{port}/push/abc",  # lokaal, buiten de store om
            "keys": {"p256dh": b64(device_pub), "auth": b64(auth_secret)},
        }
        self.assertEqual(await sender.send_one(device, "'45+1 DOELPUNT VSCO'61!", "abc123def456"), "sent")

        h = received["headers"]
        self.assertEqual((h["content-encoding"], h["ttl"], h["urgency"]), ("aes128gcm", "600", "high"))
        scheme, _, rest = h["authorization"].partition(" ")
        self.assertEqual(scheme.lower(), "vapid")
        params = dict(p.strip().split("=", 1) for p in rest.split(","))
        self.assertEqual(params["k"], sender.public_key)
        signed, signature = params["t"].rsplit(".", 1)
        self.assertTrue(Vapid02.from_raw_public(params["k"].encode()).verify_token(signed.encode(), signature))
        plain = http_ece.decrypt(received["body"], private_key=device_key, auth_secret=auth_secret,
                                 version="aes128gcm")
        self.assertEqual(json.loads(plain)["body"], "'45+1 DOELPUNT VSCO'61!")
        self.assertEqual(json.loads(plain)["url"], "/share/abc123def456")

    def test_contact_address_must_be_accepted_by_the_push_services(self):
        from push import DEFAULT_SUBJECT, validate_subject

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        vapid = load_vapid(Path(folder.name) / "v.pem")
        for good in (DEFAULT_SUBJECT, "mailto:beheer@example.com", "https://scorebord.example.nl"):
            self.assertEqual(validate_subject(vapid, good), good)
        for bad in ("https://github.com/Sholofly/VSCOLedscore", "mailto:x", "beheer@example.com", ""):
            with self.assertRaises(ValueError, msg=bad):
                validate_subject(vapid, bad)


class ConnectorNotifyTests(unittest.TestCase):
    def test_each_message_is_passed_to_the_notifier_with_its_key(self):
        calls = []

        class Notifier:
            def notify(self, text, key):
                calls.append((text, key))

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = ConnectionState()
        connector = Connector(Settings(Path(folder.name) / "s.json"), state, notifier=Notifier())
        with self.assertLogs("app", level="INFO"):
            connector._publish(["eerste", "tweede"])
        self.assertEqual(calls, [(m.text, m.key) for m in state.messages])

    def test_a_failing_notifier_does_not_stop_publishing(self):
        class Broken:
            def notify(self, text, key):
                raise RuntimeError("stuk")

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = ConnectionState()
        connector = Connector(Settings(Path(folder.name) / "s.json"), state, notifier=Broken())
        with self.assertLogs("app", level="ERROR"):
            connector._publish(["bericht"])
        self.assertEqual(len(state.messages), 1)


class PushWebTests(AioHTTPTestCase):
    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.state = ConnectionState()
        self.fake = FakePush()
        self.store = PushStore(root / "push.json")
        self.sender = PushSender(self.store, load_vapid(root / "vapid.pem"), "https://example.test", self.fake)
        settings = Settings(root / "settings.json", "wss://example.test/", "JWT x")
        return create_web_app(settings, self.state, root / "a", root / "r", "admin", "wachtwoord", push=self.sender)

    def headers(self, csrf=True):
        h = dict(AUTH)
        if csrf:
            h["X-CSRF-Token"] = self.app["csrf"]
        return h

    async def test_public_key_needs_login(self):
        self.assertEqual((await self.client.get("/push/key")).status, 401)
        data = await (await self.client.get("/push/key", headers=AUTH)).json()
        self.assertEqual(data["publicKey"], self.sender.public_key)

    async def test_subscribe_needs_csrf_and_a_valid_subscription(self):
        resp = await self.client.post("/push/subscribe", json=sub(1), headers=self.headers(csrf=False))
        self.assertEqual(resp.status, 403)
        resp = await self.client.post("/push/subscribe", json=sub(host="evil.test"), headers=self.headers())
        self.assertEqual(resp.status, 400)
        resp = await self.client.post("/push/subscribe", data="geen json", headers=self.headers())
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.store.subscriptions, [])
        resp = await self.client.post("/push/subscribe", json=sub(1), headers=self.headers())
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.store.subscriptions), 1)

    async def test_unsubscribe_and_test_notification(self):
        self.store.add(sub(1))
        resp = await self.client.post("/push/test", json={"endpoint": sub(1)["endpoint"]}, headers=self.headers())
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.fake.sent[0][1]["url"], "/")
        unknown = await self.client.post("/push/test", json={"endpoint": sub(9)["endpoint"]}, headers=self.headers())
        self.assertEqual(unknown.status, 404)
        self.fake.fail = {sub(1)["endpoint"]: 500}
        with self.assertLogs("push", level="WARNING"):
            failed = await self.client.post("/push/test", json={"endpoint": sub(1)["endpoint"]}, headers=self.headers())
        self.assertEqual(failed.status, 502)  # niet ten onrechte "verstuurd" melden
        self.fake.fail = {}
        await self.client.post("/push/unsubscribe", json={"endpoint": sub(1)["endpoint"]}, headers=self.headers())
        self.assertEqual(self.store.subscriptions, [])

    async def test_home_page_presence_suppresses_notifications_for_that_device(self):
        self.store.add(sub(1))
        self.store.add(sub(2))
        await self.client.get("/logs/data", headers={**AUTH, "X-Push-Endpoint": sub(1)["endpoint"]})
        await self.sender.send("bericht", "abc123def456")
        self.assertEqual([e for e, _, _ in self.fake.sent], [sub(2)["endpoint"]])

    async def test_share_screen_shows_one_message_ready_to_share(self):
        key = self.state.add_message("'12 DOELPUNT VSCO'61! Speler A!", "https://x.test/a.jpg")
        self.state.add_message("ander bericht")
        resp = await self.client.get(f"/share/{key}", headers=AUTH)
        body = await resp.text()
        self.assertEqual(resp.status, 200)
        self.assertIn('id="one-message"', body)
        self.assertIn("Speler A", body)
        self.assertNotIn("ander bericht", body)
        self.assertEqual(body.count('class="share"'), 1)
        self.assertIn('class="with-photo" checked', body)
        gone = await self.client.get("/share/0123456789ab", headers=AUTH, allow_redirects=False)
        self.assertEqual((gone.status, gone.headers["Location"]), (303, "/"))

    async def test_my_settings_page_has_the_notifications_card(self):
        body = await (await self.client.get("/me", headers=AUTH)).text()
        self.assertIn('id="push-toggle"', body)
        self.assertIn(f'data-csrf="{self.app["csrf"]}"', body)
        self.assertIn("pushManager.subscribe", body)

    async def test_service_worker_shows_notification_and_opens_share_screen(self):
        body = await (await self.client.get("/sw.js")).text()
        self.assertIn("addEventListener('push'", body)
        self.assertIn("showNotification", body)
        self.assertIn("addEventListener('notificationclick'", body)
        self.assertIn("openWindow", body)


class NoPushWebTests(AioHTTPTestCase):
    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        settings = Settings(root / "settings.json", "wss://example.test/", "JWT x")
        return create_web_app(settings, ConnectionState(), root / "a", root / "r", "admin", "wachtwoord")

    async def test_without_push_there_is_no_card_and_no_key(self):
        body = await (await self.client.get("/me", headers=AUTH)).text()
        self.assertNotIn('id="push-toggle"', body)
        self.assertIn("Meldingen zijn niet ingesteld", body)
        self.assertEqual((await self.client.get("/push/key", headers=AUTH)).status, 404)


if __name__ == "__main__":
    unittest.main()
