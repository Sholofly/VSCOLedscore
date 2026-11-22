import unittest
from urllib.parse import parse_qs, urlsplit

import logging
import tempfile
from pathlib import Path

import app
from settings import Settings
from state import ConnectionState
from app import MatchLifecycle, build_websocket_uri


class BuildWebsocketUriTests(unittest.TestCase):
    def test_adds_encoded_token_and_preserves_other_query_parameters(self):
        uri = build_websocket_uri(
            "wss://example.test/clubs/vsco61/?mode=live&token=old",
            "JWT new-token",
        )

        parts = urlsplit(uri)
        self.assertEqual(parts.path, "/clubs/vsco61/")
        self.assertEqual(parse_qs(parts.query), {"mode": ["live"], "token": ["JWT new-token"]})
        self.assertIn("JWT%20new-token", parts.query)

    def test_requires_a_token(self):
        with self.assertRaisesRegex(ValueError, "WS_TOKEN is empty"):
            build_websocket_uri("wss://example.test/stream", " ")

    def test_rejects_non_websocket_urls(self):
        with self.assertRaisesRegex(ValueError, "WS_URL must be"):
            build_websocket_uri("https://example.test/stream", "JWT token")


START = "2026-11-19T22:24:19Z"
END = "2026-11-19T23:10:00Z"


def trailer(home=0, away=0):
    return f"Home FC - Away FC {home}-{away}. #homawa"


class MatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = MatchLifecycle()

    @staticmethod
    def payload(
        started_at=None, ended_at=None, rest=False, home_score=0, away_score=0, seconds=0,
        period=0,
    ):
        return {
            "scoreboard_data": {
                "startedAt": started_at,
                "endedAt": ended_at,
                "rest": rest,
                "time": seconds,
                "period": period,
                "teams": {"home": 1, "away": 2},
                "scoreboard": {"home": home_score, "away": away_score},
            },
            "teams": [
                {"id": 1, "scoreboard_name": "Home FC"},
                {"id": 2, "scoreboard_name": "Away FC"},
            ],
        }

    def test_reports_start_once_in_requested_format(self):
        self.assertEqual(self.lifecycle.update(self.payload()), [])
        started = self.payload(started_at=START, seconds=3)

        self.assertEqual(
            self.lifecycle.update(started),
            [f"'1 De wedstrijd is begonnen! {trailer()}"],
        )
        self.assertEqual(self.lifecycle.update(started), [])

    def test_reports_halftime_once(self):
        self.lifecycle.update(self.payload(started_at=START, home_score=1, seconds=2700))
        halftime = self.payload(started_at=START, rest=True, home_score=1, seconds=2700)
        self.assertEqual(self.lifecycle.update(halftime), [f"'45 Rust. {trailer(1)}"])
        self.assertEqual(self.lifecycle.update(halftime), [])

    def test_reports_second_half_start_once(self):
        self.lifecycle.update(self.payload(started_at=START, rest=True))
        second_half = self.payload(started_at=START, rest=False, period=1)
        self.assertEqual(
            self.lifecycle.update(second_half),
            [f"'46 De tweede helft is begonnen! {trailer()}"],
        )
        self.assertEqual(self.lifecycle.update(second_half), [])

    def test_time_restarts_in_second_half(self):
        self.lifecycle.update(self.payload(started_at=START, period=1))
        goal = self.payload(started_at=START, away_score=1, seconds=74, period=1)
        self.assertEqual(
            self.lifecycle.update(goal), [f"'47 Doelpunt Away FC. {trailer(0, 1)}"]
        )

    def test_end_time_includes_first_half(self):
        self.lifecycle.update(self.payload(started_at=START, home_score=1, period=1))
        ended = self.payload(
            started_at=START, ended_at=END, home_score=1, seconds=2760 + 1, period=1
        )
        self.assertEqual(
            self.lifecycle.update(ended), [f"'90+2 AFGELOPEN! {trailer(1)}"]
        )

    def test_win_is_reported_in_capitals_once(self):
        self.lifecycle.update(self.payload(started_at=START, home_score=1, seconds=5000))
        ended = self.payload(
            started_at=START, ended_at=END, home_score=1, seconds=5400
        )
        self.assertEqual(
            self.lifecycle.update(ended), [f"'90 AFGELOPEN! {trailer(1)}"]
        )
        self.assertEqual(self.lifecycle.update(ended), [])

    def test_stoppage_time_is_minutes_above_regular_time(self):
        self.lifecycle.update(self.payload(started_at=START, home_score=2, seconds=5000))
        ended = self.payload(
            started_at=START, ended_at=END, home_score=2, away_score=1, seconds=5461
        )
        self.assertEqual(
            self.lifecycle.update(ended), [f"'90+2 AFGELOPEN! {trailer(2, 1)}"]
        )

    def test_draw_and_loss_are_not_in_capitals(self):
        for home, away in ((1, 1), (0, 1)):
            lifecycle = MatchLifecycle()
            lifecycle.update(self.payload(started_at=START, home_score=home, away_score=away))
            ended = self.payload(
                started_at=START, ended_at=END, home_score=home, away_score=away, seconds=5400
            )
            self.assertEqual(
                lifecycle.update(ended), [f"'90 Afgelopen! {trailer(home, away)}"]
            )

    def test_regular_minutes_is_configurable(self):
        lifecycle = MatchLifecycle(regular_minutes=60)
        lifecycle.update(self.payload(started_at=START, home_score=1))
        ended = self.payload(started_at=START, ended_at=END, home_score=1, seconds=3661)
        self.assertEqual(lifecycle.update(ended), [f"'60+2 AFGELOPEN! {trailer(1)}"])


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = MatchLifecycle()
        self.lifecycle.update(self.payload(started_at=START), now=0)

    payload = staticmethod(MatchLifecycleTests.payload)

    @staticmethod
    def with_scorer(payload, side="home", name="Speler A"):
        payload["scoreboard_data"]["scorers"] = {
            "home": [], "away": [], side: [None, {"player": {"name": name}}],
        }
        return payload

    def test_home_goal_waits_and_is_combined_with_scorer_in_one_message(self):
        goal = self.payload(started_at=START, home_score=1, seconds=125)
        self.assertEqual(self.lifecycle.update(goal, now=1), [])
        self.assertEqual(self.lifecycle.update(goal, now=5), [])
        self.assertEqual(self.lifecycle.flush_expired(now=10), [])

        later = self.with_scorer(self.payload(started_at=START, home_score=1, seconds=131))
        self.assertEqual(
            self.lifecycle.update(later, now=6),
            [f"'3 DOELPUNT Home FC! Speler A! {trailer(1)}"],
        )
        self.assertIsNone(self.lifecycle.next_deadline)
        self.assertEqual(self.lifecycle.update(later, now=7), [])
        self.assertEqual(self.lifecycle.flush_expired(now=100), [])

    def test_goal_message_carries_the_scorer_photo(self):
        goal = self.payload(started_at=START, home_score=1, seconds=30)
        goal["scoreboard_data"]["scorers"] = {
            "home": [None, {"player": {
                "name": "Speler A",
                "image": "https://api.example.test/pas.jpg",
                "goalImage": "https://api.example.test/goal.jpg",
            }}],
        }
        events = self.lifecycle.update(goal, now=1)
        self.assertEqual(events, [f"'1 DOELPUNT Home FC! Speler A! {trailer(1)}"])
        self.assertEqual(events[0].image, "https://api.example.test/goal.jpg")

    def test_photo_falls_back_to_passport_photo_and_ignores_non_urls(self):
        for player, expected in (
            ({"name": "A", "image": "https://x.test/pas.jpg"}, "https://x.test/pas.jpg"),
            ({"name": "A", "goalImage": "javascript:alert(1)", "image": ""}, None),
            ({"name": "A"}, None),
        ):
            lifecycle = MatchLifecycle()
            lifecycle.update(self.payload(started_at=START), now=0)
            goal = self.payload(started_at=START, home_score=1)
            goal["scoreboard_data"]["scorers"] = {"home": [{"player": player}]}
            (event,) = lifecycle.update(goal, now=1)
            self.assertEqual(event.image, expected)

    def test_goal_wait_is_read_per_goal_so_changes_apply_to_the_next_goal(self):
        wait = [20]
        lifecycle = MatchLifecycle(goal_wait=lambda: wait[0])
        lifecycle.update(self.payload(started_at=START), now=0)
        lifecycle.update(self.payload(started_at=START, home_score=1), now=100)
        self.assertEqual(lifecycle.next_deadline, 120)
        wait[0] = 5  # gewijzigd in de webapp
        self.assertEqual(lifecycle.next_deadline, 120)  # lopend doelpunt houdt zijn wachttijd
        lifecycle.update(self.payload(started_at=START, home_score=2), now=110)  # meldt het eerste
        self.assertEqual(lifecycle.next_deadline, 115)

    def test_zero_wait_reports_a_goal_without_scorer_right_away(self):
        lifecycle = MatchLifecycle(goal_wait=lambda: 0)
        lifecycle.update(self.payload(started_at=START), now=0)
        self.assertEqual(lifecycle.update(self.payload(started_at=START, home_score=1), now=5), [])
        self.assertEqual(lifecycle.flush_expired(now=5), [f"'1 DOELPUNT Home FC! {trailer(1)}"])

    def test_every_message_knows_its_kind(self):
        lifecycle = MatchLifecycle(goal_wait=lambda: 0)
        lifecycle.update(self.payload(), now=0)
        (start,) = lifecycle.update(self.payload(started_at=START), now=1)
        (away,) = lifecycle.update(self.payload(started_at=START, away_score=1), now=2)
        lifecycle.update(self.payload(started_at=START, away_score=1, home_score=1), now=3)
        (home,) = lifecycle.flush_expired(now=3)
        (rest,) = lifecycle.update(self.payload(started_at=START, away_score=1, home_score=1, rest=True), now=4)
        self.assertEqual(
            [m.kind for m in (start, away, home, rest)], ["start", "goal_away", "goal_home_no_scorer", "rest"]
        )
        scored = self.with_scorer(self.payload(started_at=START, home_score=2, away_score=1))
        events = lifecycle.update(scored, now=5)  # na de rust: tweede helft + doelpunt met naam
        self.assertEqual([m.kind for m in events], ["second_half", "goal_home"])

    def test_scorer_in_same_message_as_goal(self):
        goal = self.with_scorer(self.payload(started_at=START, home_score=1, seconds=30))
        self.assertEqual(
            self.lifecycle.update(goal, now=1),
            [f"'1 DOELPUNT Home FC! Speler A! {trailer(1)}"],
        )

    def test_home_goal_without_scorer_is_reported_after_20_seconds(self):
        goal = self.payload(started_at=START, home_score=1, seconds=30)
        self.lifecycle.update(goal, now=100)
        self.assertEqual(self.lifecycle.next_deadline, 120)
        self.assertEqual(self.lifecycle.flush_expired(now=119.9), [])
        self.assertEqual(
            self.lifecycle.flush_expired(now=120),
            [f"'1 DOELPUNT Home FC! {trailer(1)}"],
        )
        self.assertEqual(self.lifecycle.flush_expired(now=200), [])

    def test_away_goal_is_reported_immediately_without_scorer(self):
        goal = self.payload(started_at=START, away_score=1, seconds=600)
        self.assertEqual(
            self.lifecycle.update(goal, now=0),
            [f"'11 Doelpunt Away FC. {trailer(0, 1)}"],
        )
        self.assertIsNone(self.lifecycle.next_deadline)
        later = self.with_scorer(goal, "away", "Tegenstander")
        self.assertEqual(self.lifecycle.update(later, now=1), [])

    def test_late_home_scorer_after_timeout_is_not_reported_again(self):
        goal = self.payload(started_at=START, home_score=1)
        self.lifecycle.update(goal, now=0)
        self.lifecycle.flush_expired(now=20)
        self.assertEqual(self.lifecycle.update(self.with_scorer(goal), now=25), [])

    def test_second_home_goal_flushes_the_first(self):
        self.lifecycle.update(self.payload(started_at=START, home_score=1), now=0)
        self.assertEqual(
            self.lifecycle.update(self.payload(started_at=START, home_score=2), now=3),
            [f"'1 DOELPUNT Home FC! {trailer(1)}"],
        )
        self.assertEqual(
            self.lifecycle.flush_pending(), [f"'1 DOELPUNT Home FC! {trailer(2)}"]
        )

    def test_end_of_match_flushes_pending_goal_first(self):
        # Tweede helft (period 1): time 38:20 in de helft = minuut 84.
        self.lifecycle.update(self.payload(started_at=START, home_score=1, seconds=2300, period=1), now=0)
        ended = self.payload(started_at=START, ended_at=END, home_score=1, seconds=2700, period=1)
        self.assertEqual(
            self.lifecycle.update(ended, now=2),
            [f"'84 DOELPUNT Home FC! {trailer(1)}", f"'90 AFGELOPEN! {trailer(1)}"],
        )

    def test_stoppage_time_goals_show_per_half(self):
        # Eerste helft: na 45:00 is het blessuretijd van de eerste helft, niet minuut 46.
        for seconds, expected in ((2699, "45"), (2700, "45+1"), (2730, "45+1"), (2820, "45+3")):
            lifecycle = MatchLifecycle()
            lifecycle.update(self.payload(started_at=START, seconds=seconds), now=0)
            (event,) = lifecycle.update(
                self.payload(started_at=START, away_score=1, seconds=seconds), now=1
            )
            self.assertTrue(event.startswith(f"'{expected} Doelpunt"), (seconds, event))
        # Tweede helft: time begint opnieuw bij 0; 45:00 in de helft = 90:00 totaal.
        for seconds, expected in ((60, "47"), (2699, "90"), (2700, "90+1"), (2790, "90+2")):
            lifecycle = MatchLifecycle()
            lifecycle.update(self.payload(started_at=START, seconds=seconds, period=1), now=0)
            (event,) = lifecycle.update(
                self.payload(started_at=START, away_score=1, seconds=seconds, period=1), now=1
            )
            self.assertTrue(event.startswith(f"'{expected} Doelpunt"), (seconds, event))

    def test_stoppage_time_follows_regular_minutes(self):
        lifecycle = MatchLifecycle(regular_minutes=60)  # bv. jeugd: 2 x 30
        lifecycle.update(self.payload(started_at=START, seconds=1830), now=0)
        (event,) = lifecycle.update(self.payload(started_at=START, away_score=1, seconds=1830), now=1)
        self.assertTrue(event.startswith("'30+1 Doelpunt"), event)

    def test_score_correction_cancels_pending_goal_and_is_not_a_goal(self):
        self.lifecycle.update(self.payload(started_at=START, home_score=1), now=0)
        self.assertEqual(self.lifecycle.update(self.payload(started_at=START), now=2), [])
        self.assertIsNone(self.lifecycle.next_deadline)

    def test_malformed_scorers_are_ignored(self):
        goal = self.payload(started_at=START, home_score=1)
        goal["scoreboard_data"]["scorers"] = {"home": [None, "x", {"player": None}], "away": 5}
        self.assertEqual(self.lifecycle.update(goal, now=0), [])
        self.assertIsNotNone(self.lifecycle.next_deadline)

    def test_scorers_present_at_connect_are_not_reported(self):
        lifecycle = MatchLifecycle()
        running = self.with_scorer(self.payload(started_at=START, home_score=1))
        self.assertEqual(lifecycle.update(running, now=0), [])
        self.assertIsNone(lifecycle.next_deadline)

    def test_missing_time_gives_question_mark(self):
        goal = self.payload(started_at=START, away_score=1)
        del goal["scoreboard_data"]["time"]
        self.assertEqual(
            self.lifecycle.update(goal, now=0), [f"'? Doelpunt Away FC. {trailer(0, 1)}"]
        )

    def test_ignores_payloads_that_are_not_match_objects(self):
        for payload in ([], "text", 5, None, {"scoreboard_data": []}):
            self.assertEqual(self.lifecycle.update(payload), [])

    def test_joining_running_match_reports_nothing_until_a_change(self):
        lifecycle = MatchLifecycle()
        running = self.payload(started_at=START, home_score=1)
        self.assertEqual(lifecycle.update(running, now=0), [])
        self.assertEqual(lifecycle.update(running, now=1), [])


class RawLogTests(unittest.TestCase):
    def test_raw_messages_go_to_file_and_not_to_the_console(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sub" / "raw.log"
            app.setup_raw_log(str(path))
            handler = app.raw_logger.handlers[-1]
            try:
                self.assertFalse(app.raw_logger.propagate)
                app.raw_logger.info("%s", '{"a": 1}')
                handler.flush()
                self.assertIn('{"a": 1}', path.read_text(encoding="utf-8"))
            finally:
                app.raw_logger.removeHandler(handler)
                handler.close()


class UnwritableLogTests(unittest.TestCase):
    def test_unwritable_log_locations_do_not_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "bestand"
            blocker.write_text("x", encoding="utf-8")  # een map aanmaken onder een bestand faalt
            self.assertFalse(app.setup_raw_log(str(blocker / "sub" / "raw.log")))
            root = logging.getLogger()
            saved = root.handlers[:], root.level
            try:
                settings = Settings(Path(folder) / "s.json")
                memory = app.setup_logging(settings, blocker / "sub" / "app.log")
                self.assertEqual(len(root.handlers), 2)  # console en geheugen, geen bestand
                logging.getLogger("test_unwritable").warning("blijft zichtbaar")
                self.assertTrue(any("blijft zichtbaar" in line for line in memory.lines))
            finally:
                root.handlers[:], root.level = saved[0], saved[1]


if __name__ == "__main__":
    unittest.main()


class HashtagTests(unittest.TestCase):
    """De hashtag onder elk bericht: standaard thuis + uit, of een eigen hashtag voor één wedstrijd."""

    @staticmethod
    def payload(home_name, away_name, **kwargs):
        data = MatchLifecycleTests.payload(**kwargs)
        data["teams"] = [
            {"id": 1, "scoreboard_name": home_name},
            {"id": 2, "scoreboard_name": away_name},
        ]
        return data

    def start(self, lifecycle, home="VSCO'61", away="VSW 1"):
        lifecycle.update(self.payload(home, away))
        return lifecycle.update(self.payload(home, away, started_at=START, seconds=3))

    def test_default_is_the_first_three_letters_of_home_and_away(self):
        (message,) = self.start(MatchLifecycle())
        self.assertTrue(message.endswith("#vscvsw"), message)

    def test_the_prefix_follows_the_home_team_and_is_not_fixed_to_vsc(self):
        (message,) = self.start(MatchLifecycle(), home="Limmen", away="WSV 30")
        self.assertTrue(message.endswith("#limwsv"), message)

    def test_an_own_hashtag_replaces_the_default(self):
        lifecycle = MatchLifecycle(hashtag=lambda: "#derby")
        (message,) = self.start(lifecycle)
        self.assertTrue(message.endswith("#derby"), message)

    def test_the_own_hashtag_is_asked_for_every_message_so_a_change_counts_at_once(self):
        current = {"value": ""}
        lifecycle = MatchLifecycle(hashtag=lambda: current["value"])
        (first,) = self.start(lifecycle)
        current["value"] = "#nieuw"
        rest = lifecycle.update(self.payload("VSCO'61", "VSW 1", started_at=START, rest=True, seconds=2700))
        self.assertTrue(first.endswith("#vscvsw"))
        self.assertTrue(rest[0].endswith("#nieuw"), rest)

    def test_the_own_hashtag_is_cleared_when_the_match_ends_and_only_then(self):
        cleared = []
        lifecycle = MatchLifecycle(on_match_end=lambda: cleared.append(1))
        self.start(lifecycle)
        lifecycle.update(self.payload("VSCO'61", "VSW 1", started_at=START, home_score=1, seconds=600))
        self.assertEqual(cleared, [])
        lifecycle.update(self.payload("VSCO'61", "VSW 1", started_at=START, ended_at=END, home_score=1, seconds=5400))
        self.assertEqual(cleared, [1])
        # dezelfde eindstand nog eens geeft geen tweede keer
        lifecycle.update(self.payload("VSCO'61", "VSW 1", started_at=START, ended_at=END, home_score=1, seconds=5400))
        self.assertEqual(cleared, [1])

    def test_connector_wires_the_setting_to_the_lifecycle(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder) / "s.json")
            settings.set_hashtag("derby")
            connector = app.Connector(settings, ConnectionState())
            lifecycle = connector._lifecycle_for("wss://example.test/")
            (message,) = self.start(lifecycle)
            self.assertTrue(message.endswith("#derby"), message)
            lifecycle.update(self.payload("VSCO'61", "VSW 1", started_at=START, ended_at=END, seconds=5400))
            self.assertEqual(settings.hashtag_override, "")  # vervallen na het einde

    def test_scoreboard_end_during_an_away_match_keeps_the_own_hashtag(self):
        # Regressie: tijdens een uitduel (bron tikkieterug) speelt thuis een ander team. Het
        # eindsignaal op het scorebord mag de hashtag van de uitwedstrijd niet wissen.
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder) / "s.json")
            settings.set_hashtag("hatvsc2026")
            allowed = [False]  # bron is tikkieterug
            connector = app.Connector(settings, ConnectionState(), source_allowed=lambda: allowed[0])
            lifecycle = connector._lifecycle_for("wss://example.test/")
            self.start(lifecycle)
            lifecycle.update(self.payload("VSCO'61 2", "Ander team", started_at=START, ended_at=END, seconds=5400))
            self.assertEqual(settings.hashtag_override, "#hatvsc2026")
            allowed[0] = True  # weer een thuiswedstrijd: dan wel wissen na het einde
            lifecycle.update(self.payload("VSCO'61", "VSW 1", started_at=START, seconds=60))
            lifecycle.update(self.payload("VSCO'61", "VSW 1", started_at=START, ended_at=END, seconds=5400))
            self.assertEqual(settings.hashtag_override, "")
