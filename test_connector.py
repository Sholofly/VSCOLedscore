import asyncio
import contextlib
import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve

from app import Connector
from settings import Settings
from state import ConnectionState

START = "2026-11-19T22:24:19Z"


def status_message(away_score=0, started=True):
    return json.dumps(
        {
            "scoreboard_data": {
                "startedAt": START if started else None,
                "endedAt": None,
                "rest": False,
                "time": 30,
                "period": 0,
                "teams": {"home": 1, "away": 2},
                "scoreboard": {"home": 0, "away": away_score},
            },
            "teams": [
                {"id": 1, "scoreboard_name": "Home FC"},
                {"id": 2, "scoreboard_name": "Away FC"},
            ],
        }
    )


class FailingConnect:
    """Vervangt websockets.connect: faalt meteen, met de URI (en token) in de melding."""

    def __init__(self):
        self.calls = 0

    def __call__(self, uri, **kwargs):
        self.calls += 1
        self.uri = uri
        return self

    async def __aenter__(self):
        raise ConnectionRefusedError(f"kan {self.uri} niet bereiken")

    async def __aexit__(self, *exc):
        return False


async def wait_for(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("conditie niet bereikt binnen de tijd")
        await asyncio.sleep(0.01)


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings_path = Path(folder.name) / "settings.json"
        self.state = ConnectionState()
        self.tokens_seen: list[str] = []
        self.task: asyncio.Task | None = None

    def make_connector(self, url, token="JWT geheim-token", connect_func=None):
        settings = Settings(self.settings_path, url, token)
        kwargs = {} if connect_func is None else {"connect_func": connect_func}
        connector = Connector(settings, self.state, **kwargs)
        connector.INITIAL_DELAY = 0.05
        connector.MAX_DELAY = 0.2
        return settings, connector

    async def start(self, connector):
        self.task = asyncio.create_task(connector.run())
        self.addAsyncCleanup(self.stop)

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def server(self, handler, **kwargs):
        server = await serve(handler, "127.0.0.1", 0, **kwargs)
        self.addAsyncCleanup(self._close, server)
        return server, server.sockets[0].getsockname()[1]

    @staticmethod
    async def _close(server):
        server.close()
        await server.wait_closed()

    def record_token(self, websocket):
        query = parse_qs(urlsplit(websocket.request.path).query)
        self.tokens_seen.append(query.get("token", [""])[0])

    async def test_reconnects_after_server_closes_and_keeps_reporting(self):
        async def handler(websocket):
            first = not self.tokens_seen
            self.record_token(websocket)
            if first:
                await websocket.send(status_message(started=False))
            await websocket.send(status_message())
            await websocket.close()

        _, port = await self.server(handler)
        settings, connector = self.make_connector(f"ws://127.0.0.1:{port}/club/")
        with self.assertLogs("app", level="INFO") as logs:
            await self.start(connector)
            await wait_for(lambda: len(self.tokens_seen) >= 3)
        self.assertEqual(self.tokens_seen[0], "JWT geheim-token")
        # De lifecycle blijft bij een reconnect bewaard: de start wordt maar één keer gemeld.
        started = [l for l in logs.output if "De wedstrijd is begonnen!" in l]
        self.assertEqual(len(started), 1)
        self.assertGreaterEqual(self.state.messages_received, 3)

    async def test_unreachable_server_keeps_retrying_with_backoff_and_hides_token(self):
        failing = FailingConnect()
        settings, connector = self.make_connector(
            "ws://127.0.0.1:1/club/", connect_func=failing
        )
        waits = []
        original = connector._wait

        async def record(timeout):
            waits.append(timeout)
            return await original(timeout)

        connector._wait = record
        connector.INITIAL_DELAY, connector.MAX_DELAY = 0.1, 0.2
        with self.assertLogs("app", level="ERROR") as logs:
            await self.start(connector)
            await wait_for(lambda: len(waits) >= 4)
        self.assertEqual(self.state.status, "wachten op nieuwe poging")
        self.assertIn("ConnectionRefusedError", self.state.last_error)
        self.assertNotIn("geheim-token", self.state.last_error)
        self.assertIn("[redacted]", self.state.last_error)
        self.assertFalse(any("geheim-token" in line for line in logs.output))
        self.assertFalse(self.task.done())
        # De wachttijd loopt op (met een beetje willekeur) en wordt begrensd.
        self.assertLess(waits[0], 0.12)
        self.assertGreater(waits[1], waits[0])
        self.assertLessEqual(max(waits), 0.2 * 1.1 + 1e-9)
        self.assertGreaterEqual(failing.calls, 4)

    async def test_rejected_token_is_reported_clearly_and_retry_is_slow(self):
        async def handler(websocket):  # wordt nooit bereikt
            await websocket.wait_closed()

        def refuse(connection, request):
            self.tokens_seen.append("x")
            return connection.respond(HTTPStatus.UNAUTHORIZED, "nee\n")

        _, port = await self.server(handler, process_request=refuse)
        settings, connector = self.make_connector(f"ws://127.0.0.1:{port}/")
        with self.assertLogs("app", level="ERROR"):
            await self.start(connector)
            await wait_for(lambda: self.state.status == "wachten op nieuwe poging")
        self.assertEqual(self.state.level, "error")
        self.assertIn("token", self.state.last_error.lower())
        self.assertIn("401", self.state.last_error)

    async def test_changing_the_token_reconnects_immediately_with_the_new_token(self):
        async def handler(websocket):
            self.record_token(websocket)
            await websocket.wait_closed()

        _, port = await self.server(handler)
        settings, connector = self.make_connector(f"ws://127.0.0.1:{port}/")
        connector.INITIAL_DELAY = connector.MAX_DELAY = 30  # wijziging moet de wachttijd overslaan
        await self.start(connector)
        await wait_for(lambda: self.state.status == "verbonden")
        settings.set_token("JWT nieuw")
        await wait_for(lambda: self.tokens_seen[-1:] == ["JWT nieuw"])
        await wait_for(lambda: self.state.status == "verbonden")
        self.assertEqual(self.tokens_seen, ["JWT geheim-token", "JWT nieuw"])

    async def test_waits_while_unconfigured_and_connects_once_settings_are_filled(self):
        async def handler(websocket):
            self.record_token(websocket)
            await websocket.wait_closed()

        _, port = await self.server(handler)
        settings, connector = self.make_connector("", "")
        with self.assertLogs("app", level="ERROR"):
            await self.start(connector)
            await wait_for(lambda: self.state.status == "niet geconfigureerd")
        settings.set_url(f"ws://127.0.0.1:{port}/")
        settings.set_token("JWT ingevuld")
        await wait_for(lambda: self.state.status == "verbonden")
        self.assertEqual(self.tokens_seen, ["JWT ingevuld"])

    async def test_bad_messages_do_not_break_the_connection(self):
        async def handler(websocket):
            self.record_token(websocket)
            await websocket.send("geen json")
            await websocket.send("[1, 2]")
            await websocket.send(json.dumps({"scoreboard_data": {"scoreboard": 5, "teams": []}}))
            await websocket.send(status_message(started=False))
            await websocket.send(status_message())
            await websocket.wait_closed()

        _, port = await self.server(handler)
        settings, connector = self.make_connector(f"ws://127.0.0.1:{port}/")
        with self.assertLogs("app", level="INFO") as logs:
            await self.start(connector)
            await wait_for(lambda: any("De wedstrijd is begonnen!" in l for l in logs.output))
        self.assertEqual(len(self.tokens_seen), 1)  # nog steeds dezelfde verbinding
        self.assertTrue(any("Could not parse" in l for l in logs.output))

    async def test_goal_without_scorer_is_reported_after_the_wait_while_connected(self):
        import app

        async def handler(websocket):
            await websocket.send(status_message())
            await asyncio.sleep(0.05)
            await websocket.send(status_message(away_score=0).replace('"home": 0', '"home": 1'))
            await websocket.wait_closed()

        original = app.GOAL_WAIT_SECONDS
        app.GOAL_WAIT_SECONDS = 0.2
        self.addCleanup(setattr, app, "GOAL_WAIT_SECONDS", original)
        _, port = await self.server(handler)
        settings, connector = self.make_connector(f"ws://127.0.0.1:{port}/")
        with self.assertLogs("app", level="INFO") as logs:
            await self.start(connector)
            await wait_for(lambda: any("DOELPUNT Home FC!" in l for l in logs.output))


if __name__ == "__main__":
    unittest.main()
