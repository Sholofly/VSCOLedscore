import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from aiohttp import BasicAuth, web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

from app import Connector
from settings import Settings
from state import ConnectionState
from tikkie import (
    MAX_SLEEP_SECONDS,
    PRE_KICKOFF,
    REFRESH_SECONDS,
    TikkieClient,
    TikkieError,
    TikkieLifecycle,
    TikkieWatcher,
)
from web import create_web_app

OWN = 856
AUTH = {"Authorization": BasicAuth("admin", "wachtwoord").encode()}


def match(status="scheduled", goals=(), home_id=999, away_id=OWN, home="Hattem", away="VSCO 61", score=None):
    goals = list(goals)
    if score is None:
        score = (sum(g["side"] == "home" for g in goals), sum(g["side"] == "away" for g in goals))
    return {
        "matchId": 7242975, "source": 1, "date": "2026-11-28", "time": "14:30", "status": status,
        "homeClubId": home_id, "homeClub": home, "awayClubId": away_id, "awayClub": away,
        "homeScore": score[0], "awayScore": score[1], "goals": goals,
    }


def goal(side, n, minute, player="Speler A"):
    return {"minute": minute, "player": player, "side": side, "teamScore": n}


def settings_for(folder, **kwargs):
    s = Settings(Path(folder) / "s.json")
    s.team_id, s.team_name = OWN, "VSCO 61"
    for name, value in kwargs.items():
        setattr(s, name, value)
    return s


class ClientTests(unittest.IsolatedAsyncioTestCase):
    """De client tegen een lokale nep-API met dezelfde adressen als tikkieterug."""

    async def asyncSetUp(self):
        self.requests = []
        self.override = None

        def json_reply(request, data):
            self.requests.append((request.path_qs, dict(request.headers)))
            return self.override or web.json_response(data)

        async def clubs(request):
            return json_reply(request, [{"id": OWN, "name": "VSCO 61", "isActive": True},
                                        {"id": 5383, "name": "VCSO (VSCO ?)", "isActive": False}])

        async def programma(request):
            return json_reply(request, [{"date": "2026-11-27", "matches": [match()]},
                                        {"date": "2026-11-28", "matches": [match(home_id=1, away_id=2), match()]}])

        async def uitslagen(request):
            return json_reply(request, [{"date": "2026-11-21", "matches": [match("ended", home_id=OWN, away_id=877)]}])

        async def one_match(request):
            return json_reply(request, {**match("live"), "matchId": int(request.match_info["id"])})

        async def website(request):  # onbekende paden geven de website (HTML), zoals tikkieterug
            return web.Response(text="<!doctype html><html></html>", content_type="text/html")

        app = web.Application()
        app.add_routes([
            web.get("/clubs", clubs), web.get("/clubs/{id}/programma", programma),
            web.get("/clubs/{id}/uitslagen", uitslagen), web.get("/matches/{id}", one_match),
            web.get("/{tail:.*}", website),
        ])
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = settings_for(folder.name, tikkie_url=str(self.server.make_url("/")).rstrip("/"))
        self.client = TikkieClient(self.settings)

    async def test_search_asks_for_active_clubs_only_and_sends_no_key(self):
        clubs = await self.client.search_clubs("VSCO 61")
        self.assertEqual([c["id"] for c in clubs], [OWN])
        path, headers = self.requests[0]
        self.assertEqual(path, "/clubs?search=VSCO%2061&active=true")
        self.assertNotIn("X-Api-Key", headers)

    async def test_today_match_from_the_programme_or_the_results(self):
        found = await self.client.today_match(OWN, date(2026, 11, 28))
        self.assertEqual((found["homeClub"], found["date"]), ("Hattem", "2026-11-28"))
        played = await self.client.today_match(OWN, date(2026, 11, 21))
        self.assertEqual(played["status"], "ended")
        self.assertIsNone(await self.client.today_match(OWN, date(2026, 11, 29)))

    async def test_match_details_with_source(self):
        detail = await self.client.match(7242975, 1)
        self.assertEqual(detail["matchId"], 7242975)
        self.assertEqual(self.requests[-1][0], "/matches/7242975?source=1")

    async def test_wrong_address_server_errors_and_unreachable_give_clear_errors(self):
        self.settings.tikkie_url = str(self.server.make_url("/verkeerd")).rstrip("/")
        with self.assertRaises(TikkieError) as caught:
            await self.client.match(1, 1)
        self.assertIn("geen JSON", str(caught.exception))
        self.settings.tikkie_url = str(self.server.make_url("/")).rstrip("/")
        self.override = web.Response(status=500, text="kapot")
        with self.assertRaises(TikkieError) as caught:
            await self.client.match(1, 1)
        self.assertIn("HTTP 500", str(caught.exception))
        self.settings.tikkie_url = "https://127.0.0.1:1"
        with self.assertRaises(TikkieError):
            await self.client.match(1, 1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.lc = TikkieLifecycle(OWN)

    def run_steps(self, *snapshots):
        events = []
        for snapshot in snapshots:
            events += self.lc.update(snapshot)
        return events

    def test_full_away_match_in_real_order_without_minutes_for_moments(self):
        g1, g2, g3 = goal("away", 1, 12, "Boaz  Bruijnes"), goal("home", 1, 30), goal("away", 2, 88)
        events = self.run_steps(
            match("scheduled"), match("live"), match("live", [g1]), match("live", [g1, g2]),
            match("halftime", [g1, g2]), match("live", [g1, g2]), match("live", [g1, g2, g3]),
            match("ended", [g1, g2, g3]), match("ended", [g1, g2, g3]),
        )
        self.assertEqual([e for e in events], [
            "De wedstrijd is begonnen! Hattem - VSCO 61 0-0. #hatvsc",
            "'12 DOELPUNT VSCO 61! Boaz Bruijnes! Hattem - VSCO 61 0-1. #hatvsc",
            "'30 Doelpunt Hattem. Hattem - VSCO 61 1-1. #hatvsc",
            "Rust. Hattem - VSCO 61 1-1. #hatvsc",
            "De tweede helft is begonnen! Hattem - VSCO 61 1-1. #hatvsc",
            "'88 DOELPUNT VSCO 61! Speler A! Hattem - VSCO 61 1-2. #hatvsc",
            "AFGELOPEN! Hattem - VSCO 61 1-2. #hatvsc",
        ])
        self.assertEqual([e.kind for e in events][-1], "end_win")

    def test_hashtag_is_home_plus_away_for_every_team_not_only_for_vsco(self):
        # een ander team dan VSCO: de hashtag begint niet met vsc maar met het thuisteam
        lc = TikkieLifecycle(3)
        lc.update(match("scheduled", home_id=3, away_id=4, home="Limmen", away="WSV 30"))
        (start,) = lc.update(match("live", home_id=3, away_id=4, home="Limmen", away="WSV 30"))
        self.assertTrue(start.endswith("#limwsv"), start)
        # uitwedstrijd van datzelfde team: de thuisploeg staat voorop
        away = TikkieLifecycle(4)
        away.update(match("scheduled", home_id=3, away_id=4, home="Limmen", away="WSV 30"))
        (start,) = away.update(match("live", home_id=3, away_id=4, home="Limmen", away="WSV 30"))
        self.assertTrue(start.endswith("#limwsv"), start)

    def test_own_club_name_is_used_for_the_hashtag_like_in_the_message(self):
        lc = TikkieLifecycle(OWN, own_name=lambda: "VSCO'61")
        lc.update(match("scheduled", home_id=OWN, away_id=3, home="VSCO 61", away="ESC"))
        (start,) = lc.update(match("live", home_id=OWN, away_id=3, home="VSCO 61", away="ESC"))
        self.assertTrue(start.endswith("#vscesc"), start)

    def test_an_own_hashtag_for_the_match_replaces_the_default_and_is_cleared_at_the_end(self):
        cleared = []
        lc = TikkieLifecycle(OWN, hashtag=lambda: "#derby", on_end=lambda: cleared.append(1))
        g = goal("away", 1, 12)
        events = []
        for snapshot in (match("scheduled"), match("live"), match("live", [g]), match("ended", [g])):
            events += lc.update(snapshot)
        self.assertTrue(events)
        self.assertTrue(all(e.endswith("#derby") for e in events), events)
        self.assertEqual(cleared, [1])
        lc.update(match("ended", [g]))
        self.assertEqual(cleared, [1])  # niet nog eens

    def test_the_watcher_wires_the_setting_to_the_lifecycle(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = settings_for(folder)
            settings.set_hashtag("derby")
            watcher = TikkieWatcher(settings, lambda messages: None, client=object())
            lifecycle = watcher._lifecycle_for(match())
            lifecycle.update(match("scheduled"))
            (start,) = lifecycle.update(match("live"))
            self.assertTrue(start.endswith("#derby"), start)
            lifecycle.update(match("ended"))
            self.assertEqual(settings.hashtag_override, "")

    def test_first_snapshot_is_the_baseline(self):
        self.assertEqual(self.lc.update(match("live", [goal("away", 1, 10)])), [])
        self.assertEqual(self.lc.update(match("live", [goal("away", 1, 10)])), [])
        self.assertEqual(len(self.lc.update(match("live", [goal("away", 1, 10), goal("home", 1, 20)]))), 1)

    def test_own_club_name_override_and_home_match_perspective(self):
        lc = TikkieLifecycle(OWN, own_name=lambda: "VSCO'61")
        lc.update(match("live", home_id=OWN, away_id=3, home="VSCO 61", away="ESC"))
        (event,) = lc.update(match("live", [goal("home", 1, 5)], home_id=OWN, away_id=3, home="VSCO 61", away="ESC"))
        self.assertEqual(event, "'5 DOELPUNT VSCO'61! Speler A! VSCO'61 - ESC 1-0. #vscesc")

    def test_unknown_scorer_and_score_without_goal_entry(self):
        self.lc.update(match("live"))
        (unknown,) = self.lc.update(match("live", [goal("away", 1, 20, "Onbekende   Speler")]))
        self.assertEqual(unknown.kind, "goal_home_no_scorer")
        self.assertTrue(unknown.startswith("'20 DOELPUNT VSCO 61!"))
        # Alleen de stand is bijgewerkt (nog geen doelpunt in de lijst): melding zonder minuut.
        only_score = match("live", [goal("away", 1, 20, "Onbekende Speler")], score=(1, 1))
        (opp,) = self.lc.update(only_score)
        self.assertEqual(opp, "Doelpunt Hattem. Hattem - VSCO 61 1-1. #hatvsc")
        # Later komt het doelpunt alsnog in de lijst: geen tweede melding.
        later = match("live", [goal("away", 1, 20, "Onbekende Speler"), goal("home", 1, 40)])
        self.assertEqual(self.lc.update(later), [])

    def test_several_goals_in_one_poll_get_their_own_score(self):
        self.lc.update(match("live"))
        events = self.lc.update(match("live", [goal("away", 1, 3), goal("home", 1, 4), goal("away", 2, 5)]))
        self.assertEqual([e.rsplit(" ", 2)[1].rstrip(".") for e in events], ["0-1", "1-1", "1-2"])

    def test_corrections_do_not_repeat_and_loss_is_not_in_capitals(self):
        g = goal("home", 1, 10)
        self.run_steps(match("live"), match("live", [g]))
        self.assertEqual(self.lc.update(match("live")), [])  # doelpunt weggehaald
        self.assertEqual(self.lc.update(match("live", [g])), [])  # en weer terug: niet opnieuw
        (end,) = self.lc.update(match("ended", [g]))
        self.assertEqual((end, end.kind), ("Afgelopen! Hattem - VSCO 61 1-0. #hatvsc", "end_other"))

    def test_jump_from_scheduled_to_halftime_reports_start_and_rest(self):
        events = self.run_steps(match("scheduled"), match("halftime"))
        self.assertEqual([e.kind for e in events], ["start", "rest"])


class FakeClient:
    def __init__(self, today=None, details=()):
        self.today = today
        self.details = list(details)
        self.match_calls = 0
        self.fail = None

    async def today_match(self, team_id, day):
        if self.fail:
            raise self.fail
        return self.today

    async def match(self, match_id, source):
        self.match_calls += 1
        return self.details.pop(0) if len(self.details) > 1 else self.details[0]


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = settings_for(folder.name)
        self.published = []
        self.clock = datetime(2026, 11, 28, 14, 20)

    def watcher(self, client):
        return TikkieWatcher(self.settings, self.published.extend, client, now=lambda: self.clock)

    async def test_not_configured_waits_for_settings(self):
        self.settings.team_id = None
        w = self.watcher(FakeClient())
        self.assertIsNone(await w.step())
        self.assertEqual(w.active_source(), "scoreboard")
        self.assertIn("niet ingesteld", w.state.status)

    async def test_no_match_today(self):
        w = self.watcher(FakeClient(today=None))
        self.assertEqual(await w.step(), MAX_SLEEP_SECONDS)
        self.assertEqual(w.state.status, "geen wedstrijd vandaag")

    async def test_home_match_in_auto_mode_uses_the_scoreboard(self):
        client = FakeClient(today=match(home_id=OWN, away_id=3), details=[match("live")])
        w = self.watcher(client)
        await w.step()
        self.assertEqual(w.active_source(), "scoreboard")
        self.assertEqual(client.match_calls, 0)

    async def test_away_match_waits_for_kickoff_then_polls_and_stops_after_the_end(self):
        g = goal("away", 1, 10)
        client = FakeClient(today=match(), details=[match("scheduled"), match("live"), match("live", [g]), match("ended", [g])])
        w = self.watcher(client)
        self.clock = datetime(2026, 11, 28, 13, 0)
        wait = await w.step()
        self.assertEqual(w.active_source(), "tikkieterug")
        self.assertEqual(client.match_calls, 0)
        self.assertEqual(wait, MAX_SLEEP_SECONDS)  # nooit lang slapen: elke minuut op de echte klok kijken
        self.clock = datetime(2026, 11, 28, 14, 16)
        for _ in range(4):
            wait = await w.step()
        self.assertEqual([e.kind for e in self.published], ["start", "goal_home", "end_win"])
        self.assertEqual(wait, MAX_SLEEP_SECONDS)
        await w.step()
        self.assertEqual(client.match_calls, 4)  # na het einde niet meer bevragen
        self.assertEqual(w.state.status, "wedstrijd afgelopen")

    async def test_after_sleep_mode_polling_starts_within_a_minute(self):
        # Regressie: de pc sliep tijdens de aftrap; de app (in de Docker-VM) stond stil en
        # een lange timer liep daarna nog minutenlang door. Nu slaapt de watcher hooguit
        # een minuut en kijkt hij dan op de echte klok.
        client = FakeClient(today=match(), details=[match("live", [goal("home", 1, 5)])])
        w = self.watcher(client)
        self.clock = datetime(2026, 11, 28, 12, 0)
        self.assertLessEqual(await w.step(), MAX_SLEEP_SECONDS)
        self.assertEqual(client.match_calls, 0)
        self.clock = datetime(2026, 11, 28, 14, 55)  # wakker na de slaapstand: klok sprong vooruit
        self.assertEqual(await w.step(), self.settings.poll_seconds)
        self.assertEqual(client.match_calls, 1)  # meteen bevraagd
        self.assertEqual(w.state.status, "volgt de uitwedstrijd")

    async def test_programme_is_refreshed_on_the_real_clock(self):
        calls = []

        class Counting(FakeClient):
            async def today_match(self, team_id, day):
                calls.append(day)
                return None

        w = self.watcher(Counting())
        self.clock = datetime(2026, 11, 28, 10, 0)
        await w.step()
        self.clock = datetime(2026, 11, 28, 10, 5)
        await w.step()
        self.assertEqual(len(calls), 1)  # binnen 10 minuten niet opnieuw
        self.clock = datetime(2026, 11, 28, 10, 11)
        await w.step()
        self.clock = datetime(2026, 11, 29, 10, 11, 30)  # nieuwe dag
        await w.step()
        self.assertEqual(calls, [date(2026, 11, 28)] * 2 + [date(2026, 11, 29)])

    async def test_poll_interval_comes_from_the_settings(self):
        self.settings.poll_seconds = 45
        w = self.watcher(FakeClient(today=match(), details=[match("live")]))
        self.assertEqual(await w.step(), 45)
        self.assertEqual(w.state.status, "volgt de uitwedstrijd")

    async def test_manual_override(self):
        client = FakeClient(today=match(home_id=OWN, away_id=3), details=[match("live", home_id=OWN, away_id=3)])
        w = self.watcher(client)
        self.settings.source_mode = "tikkieterug"
        await w.step()
        self.assertEqual((w.active_source(), client.match_calls), ("tikkieterug", 1))
        away = self.watcher(FakeClient(today=match(), details=[match("live")]))
        self.settings.source_mode = "scoreboard"
        await away.step()
        self.assertEqual(away.active_source(), "scoreboard")

    async def test_errors_are_shown_and_retried(self):
        client = FakeClient()
        client.fail = TikkieError("tikkieterug weigert de sleutel (HTTP 403).")
        w = self.watcher(client)
        with self.assertLogs("tikkie", level="WARNING"):
            wait = await w.step()
        self.assertGreater(wait, 0)
        self.assertEqual(w.state.level, "error")
        self.assertIn("HTTP 403", w.state.last_error)

    async def test_run_loop_stops_cleanly(self):
        w = self.watcher(FakeClient(today=None))
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.05)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        self.assertIn(task, done)


class ConnectorSourceTests(unittest.TestCase):
    def test_scoreboard_messages_are_ignored_while_tikkieterug_is_the_source(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = ConnectionState()
        allowed = [False]
        connector = Connector(Settings(Path(folder.name) / "s.json"), state, source_allowed=lambda: allowed[0])
        with self.assertLogs("app", level="INFO") as logs:
            connector._publish(["'1 De wedstrijd is begonnen!"])
        self.assertEqual(len(state.messages), 0)
        self.assertIn("genegeerd", logs.output[0])
        with self.assertLogs("app", level="INFO"):
            connector.publish_events(["van tikkieterug"])  # de watcher publiceert altijd
        allowed[0] = True
        with self.assertLogs("app", level="INFO"):
            connector._publish(["van het scorebord"])
        self.assertEqual([m.text for m in state.messages], ["van tikkieterug", "van het scorebord"])


class TikkieSettingsTests(unittest.TestCase):
    def test_values_are_validated_saved_and_signal_the_watcher(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "s.json"
        s = Settings(path)
        self.assertFalse(s.tikkie_ready)
        s.set_team("856", "VSCO  61")
        s.set_source_mode("tikkieterug")
        s.set_poll_seconds("45")
        s.set_club_name("VSCO'61")
        self.assertTrue(s.tikkie_changed.is_set())
        again = Settings(path)
        self.assertEqual((again.team_id, again.team_name, again.source_mode, again.poll_seconds,
                          again.club_name), (856, "VSCO 61", "tikkieterug", 45, "VSCO'61"))
        self.assertTrue(again.tikkie_ready)
        for setter, bad in ((s.set_poll_seconds, "5"), (s.set_poll_seconds, "x"), (s.set_source_mode, "beide"),
                            (s.set_team, "-1"), (s.set_tikkie_url, "http://x"),
                            (s.set_club_name, "x" * 61)):
            with self.assertRaises(ValueError, msg=str(bad)):
                setter(bad, "naam") if setter.__name__ == "set_team" else setter(bad)

    def test_defaults_need_no_key_only_a_team(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        s = Settings(Path(folder.name) / "s.json")
        self.assertEqual((s.tikkie_url, s.source_mode, s.poll_seconds), ("https://tikkieterug.sholofly.nl", "auto", 30))
        self.assertFalse(s.tikkie_ready)
        s.set_team(OWN, "VSCO 61")
        self.assertTrue(s.tikkie_ready)
        self.assertNotIn("key", json.dumps(json.loads(Path(folder.name, "s.json").read_text(encoding="utf-8"))))


class FakeSearchClient:
    async def search_clubs(self, query):
        return [{"id": 856, "name": "VSCO 61", "speeldag": "Zaterdag", "competitionName": "Oost 4C ZA"}]


class TikkieWebTests(AioHTTPTestCase):
    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.settings = Settings(root / "settings.json", "wss://example.test/", "JWT x")
        self.watcher = TikkieWatcher(self.settings, lambda events: None, FakeSearchClient())
        return create_web_app(self.settings, ConnectionState(), root / "a", root / "r", "admin", "wachtwoord",
                              tikkie=self.watcher, share_user="deler", share_password="dp")

    def post(self, **fields):
        return self.client.post("/settings", data={"csrf": self.app["csrf"], **fields}, headers=AUTH)

    async def test_block_is_on_the_app_page_without_a_key_field(self):
        body = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn('<details class="block" id="uitwedstrijden"', body)
        self.assertIn('name="tikkie_url"', body)
        self.assertNotIn("tikkie_key", body)
        self.assertIn("geen sleutel nodig", body)

    async def test_search_and_choose_a_team(self):
        found = await (await self.post(action="team_search", q="VSCO")).text()
        self.assertIn("VSCO 61 · Zaterdag · Oost 4C ZA", found)
        self.assertIn('name="team_id" value="856"', found)
        resp = await self.post(action="team", team_id="856", team_name="VSCO 61")
        self.assertEqual(resp.status, 200)
        self.assertEqual((self.settings.team_id, self.settings.team_name), (856, "VSCO 61"))
        self.assertIn("Team opgeslagen", await resp.text())

    async def test_source_and_interval_and_errors(self):
        await self.post(action="source", source_mode="tikkieterug")
        self.assertEqual(self.settings.source_mode, "tikkieterug")
        bad = await self.post(action="poll", poll="2")
        self.assertEqual(bad.status, 400)
        self.assertIn('id="uitwedstrijden" open', await bad.text())
        self.assertEqual(self.settings.poll_seconds, 30)

    async def test_led_and_status_follow_tikkieterug_during_an_away_match(self):
        self.settings.team_id = OWN
        self.watcher.match = match()
        self.watcher.state.set("volgt de uitwedstrijd", "ok")
        data = await (await self.client.get("/logs/data", headers=AUTH)).json()
        self.assertEqual(data["led"], {"level": "ok", "status": "uitduel: volgt de uitwedstrijd"})
        self.assertIn("tikkieterug (uitwedstrijd)", data["status_html"])

    async def test_a_user_cannot_change_tikkieterug(self):
        user = {"Authorization": BasicAuth("deler", "dp").encode()}
        resp = await self.client.post("/settings", data={"csrf": self.app["csrf"], "action": "tikkie_url",
                                                         "tikkie_url": "https://evil.test"}, headers=user)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.settings.tikkie_url, "https://tikkieterug.sholofly.nl")


if __name__ == "__main__":
    unittest.main()
