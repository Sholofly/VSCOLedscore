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
        self.assertEqual([s.label for s in sources.values()], ["LED Scores", "Voetbalnederland"])
        for source in sources.values():
            self.assertTrue(source.description)
            self.assertNotIn(SERVER_NAME, (source.label + source.description).lower())


class ClubTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = Settings(Path(folder.name) / "s.json")
        state = ConnectionState()
        self.sources = build_sources(self.settings, state, Publisher(state).publish)

    def test_scoreboard_club_is_the_home_team_of_the_last_status(self):
        source = self.sources["scoreboard"]
        self.assertIsNone(source.club())
        self.assertIn("thuisploeg", source.club_hint)
        lifecycle = source.connector._lifecycle_for(self.settings.url)
        lifecycle.update({
            "teams": [{"id": 1, "scoreboard_name": "Thuisclub"}, {"id": 2, "scoreboard_name": "Gasten"}],
            "scoreboard_data": {"teams": {"home": 1, "away": 2}, "scoreboard": {"home": 0, "away": 0}},
        })
        self.assertEqual(source.club(), "Thuisclub")
        self.assertEqual(source.match, {"homeClub": "Thuisclub", "awayClub": "Gasten"})

    def test_voetbalnederland_club_is_the_chosen_team_or_its_own_name(self):
        source = self.sources["voetbalnederland"]
        self.assertIsNone(source.club())
        self.settings.team_id, self.settings.team_name = 856, "VSCO 61"
        self.assertEqual(source.club(), "VSCO 61")
        self.settings.club_name = "VSCO'61"
        self.assertEqual(source.club(), "VSCO'61")


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

    async def test_source_and_club_are_saved_together(self):
        resp = await self.post(action="source", source="voetbalnederland", team="856:VSCO 61")
        self.assertEqual((resp.status, resp.headers["Location"]), (303, "/app?msg=source#bron"))
        self.assertEqual(self.settings.source, "voetbalnederland")
        self.assertEqual((self.settings.team_id, self.settings.team_name), (856, "VSCO 61"))
        self.assertTrue(self.settings.source_changed.is_set())

    async def test_voetbalnederland_without_a_club_is_refused_and_the_choice_stays(self):
        resp = await self.post(action="source", source="voetbalnederland")
        self.assertEqual(resp.status, 400)
        body = await resp.text()
        self.assertIn("Kies je club bij Voetbalnederland.", body)
        self.assertIn('name="source" value="voetbalnederland" checked', body)
        self.assertEqual(self.settings.source, "scoreboard")

    async def test_saving_the_same_source_does_not_restart_it(self):
        resp = await self.post(action="source", source="scoreboard")
        self.assertEqual(resp.status, 303)
        self.assertFalse(self.settings.source_changed.is_set())

    async def test_unknown_source_is_refused(self):
        resp = await self.post(action="source", source="onbekend")
        self.assertEqual(resp.status, 400)
        self.assertIn('id="bron" open', await resp.text())
        self.assertEqual(self.settings.source, "scoreboard")

    async def test_only_the_settings_of_the_chosen_source_are_shown(self):
        body = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn('id="scorebord"', body)
        self.assertNotIn('id="voetbalnederland"', body)
        self.settings.source = "voetbalnederland"
        body = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn('id="voetbalnederland"', body)
        self.assertNotIn('id="scorebord"', body)
        # Een melding of fout voor de andere bron opent dat blok toch, zodat hij zichtbaar is.
        body = await (await self.client.get("/app?msg=goal_wait", headers=AUTH)).text()
        self.assertIn('<details class="block" id="scorebord" open>', body)

    async def test_source_and_club_come_first_and_the_club_is_fixed_for_the_scoreboard(self):
        body = await (await self.client.get("/app", headers=AUTH)).text()
        # Bron en club is het eerste blok en staat standaard open.
        self.assertLess(body.index('id="bron"'), body.index('id="status"'))
        # De instellingen van de gekozen bron staan direct onder Bron en club, voor de status.
        self.assertLess(body.index('id="bron"'), body.index('id="scorebord"'))
        self.assertLess(body.index('id="scorebord"'), body.index('id="status"'))
        self.assertIn('<details class="block" id="bron" open><summary>Bron en club</summary>', body)
        self.assertIn('<div class="club" data-source="scoreboard"><p><span class="muted">nog onbekend</span>', body)
        self.manager.sources["scoreboard"].club = lambda: "Thuisclub"
        body = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn('<div class="club" data-source="scoreboard"><p><strong>Thuisclub</strong></p>', body)
        self.assertIn("<th>Je club</th><td>Thuisclub</td>", body)
        # Het clubdeel van de andere bron staat klaar maar verborgen; één knop slaat alles op.
        self.assertIn('<div class="club" data-source="voetbalnederland" hidden>', body)
        self.assertEqual(body.count('name="save"'), 1)

    async def test_hashtag_block_names_the_match(self):
        body = await (await self.client.get("/sjablonen", headers=AUTH)).text()
        self.assertIn("Wedstrijd: nog niet bekend bij de bron.", body)
        self.assertNotIn("deze wedstrijd (met", body)

    async def test_app_is_called_matchshare(self):
        body = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn("<title>Connectie - MatchShareNL</title>", body)
        manifest = await (await self.client.get("/manifest.webmanifest")).json(content_type=None)
        self.assertEqual((manifest["name"], manifest["short_name"]), ("MatchShareNL", "MatchShareNL"))
        sw = await (await self.client.get("/sw.js")).text()
        self.assertIn("'MatchShareNL'", sw)
        for text in (body, sw, str(manifest)):
            self.assertNotIn("VSCO scorebord", text)
            self.assertNotIn(SERVER_NAME, text.lower().replace(SERVER_HOST, ""))


if __name__ == "__main__":
    unittest.main()
