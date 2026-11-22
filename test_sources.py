import asyncio
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import BasicAuth
from aiohttp.test_utils import AioHTTPTestCase

from settings import DEFAULT_VOETBALNEDERLAND_URL, Settings
from sources import Publisher, Source, SourceManager, build_sources
from state import ConnectionState
from web import create_web_app

# De naam van de API-server mag nergens in de app staan, alleen in het standaardadres.
SERVER_HOST = urlparse(DEFAULT_VOETBALNEDERLAND_URL).hostname
SERVER_NAME = SERVER_HOST.split(".")[0][:6]

AUTH = {"Authorization": BasicAuth("admin", "wachtwoord").encode()}


class FakeSource(Source):
    def __init__(self, source_id, label, log, fail=False):
        super().__init__(lambda events: None)
        self.id, self.label, self.description = source_id, label, f"Beschrijving van {label}"
        self.log, self.fail = log, fail

    async def run(self):
        self.log.append(("start", self.id))
        try:
            if self.fail:
                raise RuntimeError("stuk")
            await asyncio.Event().wait()
        finally:
            self.log.append(("stop", self.id))

    def led(self):
        return "ok", f"{self.label} draait"


async def wait_for(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("conditie niet bereikt")
        await asyncio.sleep(0.01)


class RegistryTests(unittest.TestCase):
    def test_scoreboard_and_voetbalnederland_are_available_in_that_order(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder) / "s.json")
            state = ConnectionState()
            sources = build_sources(settings, state, Publisher(state).publish)
        self.assertEqual(list(sources), ["scoreboard", "voetbalnederland"])
        self.assertEqual([s.label for s in sources.values()], ["Scorebord", "Voetbalnederland"])
        for source in sources.values():
            self.assertTrue(source.description)
            self.assertNotIn(SERVER_NAME, (source.label + source.description).lower())


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = Settings(Path(folder.name) / "s.json")
        self.log = []

    def manager(self, fail=False):
        sources = {"scoreboard": FakeSource("scoreboard", "Scorebord", self.log),
                   "voetbalnederland": FakeSource("voetbalnederland", "Voetbalnederland", self.log, fail)}
        return SourceManager(self.settings, sources)

    async def run_manager(self, manager):
        task = asyncio.create_task(manager.run())

        async def stop():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    async def test_only_the_chosen_source_runs_and_switching_stops_the_old_one(self):
        manager = self.manager()
        await self.run_manager(manager)
        await wait_for(lambda: self.log == [("start", "scoreboard")])
        self.assertEqual(manager.active().led(), ("ok", "Scorebord draait"))
        self.settings.set_source("voetbalnederland")
        await wait_for(lambda: ("start", "voetbalnederland") in self.log)
        self.assertEqual(self.log, [("start", "scoreboard"), ("stop", "scoreboard"), ("start", "voetbalnederland")])
        self.assertEqual(manager.active().led(), ("ok", "Voetbalnederland draait"))

    async def test_unknown_choice_falls_back_to_the_scoreboard(self):
        self.settings.source = "bestaat_niet"
        self.assertEqual(self.manager().active_id, "scoreboard")

    async def test_a_crashing_source_is_logged_and_restarted(self):
        manager = self.manager(fail=True)
        manager.RESTART_DELAY = 0.01
        self.settings.source = "voetbalnederland"
        with self.assertLogs("sources", level="ERROR"):
            await self.run_manager(manager)
            await wait_for(lambda: self.log.count(("start", "voetbalnederland")) >= 2)

    async def test_stopping_the_manager_stops_the_source(self):
        manager = self.manager()
        task = asyncio.create_task(manager.run())
        await wait_for(lambda: self.log == [("start", "scoreboard")])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.log[-1], ("stop", "scoreboard"))
        self.assertTrue(task.done())

    async def test_choice_is_saved(self):
        self.settings.set_source("voetbalnederland")
        self.assertEqual(Settings(self.settings.path).source, "voetbalnederland")
        with self.assertRaises(ValueError):
            self.settings.set_source("geen geldige naam!")


class SourceWebTests(AioHTTPTestCase):
    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.settings = Settings(root / "settings.json", "wss://example.test/", "JWT x")
        self.log = []
        self.manager = SourceManager(self.settings, {
            "scoreboard": FakeSource("scoreboard", "Scorebord", self.log),
            "voetbalnederland": FakeSource("voetbalnederland", "Voetbalnederland", self.log),
        })
        return create_web_app(self.settings, ConnectionState(), root / "a", root / "r", "admin", "wachtwoord",
                              sources=self.manager)

    def post(self, **fields):
        return self.client.post("/settings", data={"csrf": self.app["csrf"], **fields}, headers=AUTH,
                                allow_redirects=False)

    async def test_source_block_lists_every_registered_source(self):
        body = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn('<details class="block" id="bron"', body)
        self.assertIn('name="source" value="scoreboard" checked', body)
        self.assertIn('name="source" value="voetbalnederland">', body)
        self.assertIn("Beschrijving van Voetbalnederland", body)
        self.assertIn("<th>Bron</th><td>Scorebord</td>", body)

    async def test_choosing_a_source(self):
        resp = await self.post(action="source", source="voetbalnederland")
        self.assertEqual((resp.status, resp.headers["Location"]), (303, "/app?msg=source#bron"))
        self.assertEqual(self.settings.source, "voetbalnederland")
        self.assertTrue(self.settings.source_changed.is_set())

    async def test_unknown_source_is_refused(self):
        resp = await self.post(action="source", source="onbekend")
        self.assertEqual(resp.status, 400)
        self.assertIn('id="bron" open', await resp.text())
        self.assertEqual(self.settings.source, "scoreboard")

    async def test_app_is_called_matchshare(self):
        body = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn("<title>App - Matchshare</title>", body)
        manifest = await (await self.client.get("/manifest.webmanifest")).json(content_type=None)
        self.assertEqual((manifest["name"], manifest["short_name"]), ("Matchshare", "Matchshare"))
        sw = await (await self.client.get("/sw.js")).text()
        self.assertIn("'Matchshare'", sw)
        for text in (body, sw, str(manifest)):
            self.assertNotIn("VSCO scorebord", text)
            self.assertNotIn(SERVER_NAME, text.lower().replace(SERVER_HOST, ""))


if __name__ == "__main__":
    unittest.main()
