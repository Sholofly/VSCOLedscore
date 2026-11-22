import base64
import json
import tempfile
import unittest
from pathlib import Path

from messages import DEFAULT_TEMPLATES, render, render_message, validate_template
from settings import Settings, token_expiry
from sources.scoreboard import MatchLifecycle

import test_app
import test_app
from messages import default_hashtag, hashtag_part, validate_hashtag


class MessageTemplateTests(unittest.TestCase):
    def test_render_fills_placeholders_once_without_reexpanding_values(self):
        text = render("{speler} scoort {thuis}", {"speler": "{thuis}", "thuis": "VSCO"})
        self.assertEqual(text, "{thuis} scoort VSCO")

    def test_unknown_placeholders_are_left_alone_when_rendering(self):
        self.assertEqual(render("{onbekend}", {}), "{onbekend}")

    def test_validate_rejects_unknown_or_unavailable_placeholders(self):
        self.assertIsNone(validate_template("goal_home", "{speler} {afsluiting}"))
        self.assertIn("{speler}", validate_template("goal_away", "{speler}"))
        self.assertIn("{nonsens}", validate_template("start", "{nonsens}"))
        self.assertIn("{afsluiting}", validate_template("afsluiting", "{afsluiting}"))
        self.assertIsNotNone(validate_template("start", "a\nb"))
        self.assertIsNotNone(validate_template("start", "x" * 501))
        self.assertIsNotNone(validate_template("bestaat_niet", "x"))

    def test_trailer_is_inserted_and_empty_template_gives_no_message(self):
        context = {"thuis": "A", "uit": "B", "stand": "1-0", "hashtag": "#vscbbb", "minuut": "5"}
        self.assertEqual(
            render_message(DEFAULT_TEMPLATES, "start", context),
            "'5 De wedstrijd is begonnen! A - B 1-0. #vscbbb",
        )
        self.assertEqual(render_message({"start": ""}, "start", context), "")


class CustomTemplateTests(unittest.TestCase):
    def test_lifecycle_uses_changed_templates_live_and_skips_empty_ones(self):
        templates = dict(DEFAULT_TEMPLATES)
        lifecycle = MatchLifecycle(templates=lambda: templates)
        lifecycle.update(test_app.GoalTests.payload(started_at=None), now=0)
        templates["start"] = "Aftrap! {stand} {afsluiting}"
        templates["afsluiting"] = "{hashtag}"
        started = test_app.GoalTests.payload(started_at=test_app.START)
        self.assertEqual(lifecycle.update(started, now=1), ["Aftrap! 0-0 #homawa"])

        templates["goal_away"] = ""
        goal = test_app.GoalTests.payload(started_at=test_app.START, away_score=1)
        self.assertEqual(lifecycle.update(goal, now=2), [])

    def test_template_error_falls_back_to_default(self):
        lifecycle = MatchLifecycle(templates=lambda: 1 / 0)
        lifecycle.update(test_app.GoalTests.payload(), now=0)
        with self.assertLogs("sources", level="ERROR"):
            events = lifecycle.update(test_app.GoalTests.payload(started_at=test_app.START), now=1)
        self.assertEqual(len(events), 1)
        self.assertIn("De wedstrijd is begonnen!", events[0])


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "data" / "settings.json"

    def make(self, url="wss://example.test/x", token="JWT env-token"):
        return Settings(self.path, url, token)

    def test_env_values_are_defaults_and_saved_values_win(self):
        settings = self.make()
        self.assertEqual((settings.url, settings.token), ("wss://example.test/x", "JWT env-token"))
        settings.set_url("wss://other.test/y")
        settings.set_token("JWT new")
        again = self.make()
        self.assertEqual((again.url, again.token), ("wss://other.test/y", "JWT new"))

    def test_changing_url_or_token_signals_a_reconnect_but_templates_do_not(self):
        settings = self.make()
        settings.set_templates({"start": "x"})
        self.assertFalse(settings.connection_changed.is_set())
        settings.set_token("JWT other")
        self.assertTrue(settings.connection_changed.is_set())
        settings.connection_changed.clear()
        settings.set_token("JWT other")  # ongewijzigd
        self.assertFalse(settings.connection_changed.is_set())
        settings.set_url("wss://other.test/z")
        self.assertTrue(settings.connection_changed.is_set())

    def test_invalid_url_and_token_are_rejected_and_nothing_is_saved(self):
        settings = self.make()
        for bad in ("https://example.test", "wss://", "wss://a b.test", "wss://x.test/?token=abc", ""):
            with self.assertRaises(ValueError, msg=bad):
                settings.set_url(bad)
        for bad in ("", "   ", "a\nb", "x" * 5000):
            with self.assertRaises(ValueError):
                settings.set_token(bad)
        self.assertFalse(self.path.exists())
        self.assertEqual(settings.url, "wss://example.test/x")

    def test_templates_store_only_differences_and_can_be_reset(self):
        settings = self.make()
        settings.set_templates({**DEFAULT_TEMPLATES, "start": "Aftrap! {afsluiting}"})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["templates"], {"start": "Aftrap! {afsluiting}"})
        self.assertEqual(self.make().templates["start"], "Aftrap! {afsluiting}")
        with self.assertRaises(ValueError):
            settings.set_templates({"goal_away": "{speler}"})
        self.assertEqual(settings.templates["goal_away"], DEFAULT_TEMPLATES["goal_away"])
        settings.reset_templates()
        self.assertEqual(self.make().templates, DEFAULT_TEMPLATES)

    def test_corrupt_or_invalid_file_falls_back_to_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{kapot", encoding="utf-8")
        with self.assertLogs("settings", level="ERROR"):
            settings = self.make()
        self.assertEqual(settings.url, "wss://example.test/x")

        self.path.write_text(json.dumps({"templates": {"start": "{nonsens}", "x": "y"}}), encoding="utf-8")
        self.assertEqual(self.make().templates, DEFAULT_TEMPLATES)

    def test_goal_wait_defaults_to_20_validates_and_is_saved(self):
        settings = self.make()
        self.assertEqual(settings.goal_wait_seconds, 20)
        settings.set_goal_wait("45")
        self.assertEqual(self.make().goal_wait_seconds, 45)
        for bad in ("", "abc", "-1", "301", "2.5", None):
            with self.assertRaises(ValueError, msg=bad):
                settings.set_goal_wait(bad)
        self.assertEqual(settings.goal_wait_seconds, 45)
        settings.set_goal_wait(0)  # 0 = niet wachten
        self.assertEqual(self.make().goal_wait_seconds, 0)

    def test_changing_goal_wait_does_not_reconnect(self):
        settings = self.make()
        settings.set_goal_wait(30)
        self.assertFalse(settings.connection_changed.is_set())

    def test_invalid_goal_wait_in_file_falls_back_to_default(self):
        self.path.parent.mkdir(parents=True)
        for bad in (9999, "x", True, -5):
            self.path.write_text(json.dumps({"goal_wait_seconds": bad}), encoding="utf-8")
            with self.assertNoLogs("settings", level="CRITICAL"):
                self.assertEqual(self.make().goal_wait_seconds, 20, bad)

    def test_no_temporary_files_are_left_behind(self):
        self.make().set_token("JWT abc")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["settings.json"])


class TokenExpiryTests(unittest.TestCase):
    @staticmethod
    def jwt(claims):
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"JWT aaa.{body}.sig"

    def test_reads_only_the_expiry(self):
        expiry = token_expiry(self.jwt({"exp": 1893456000, "sub": "geheim"}))
        self.assertEqual(expiry.year, 2030)

    def test_returns_none_for_tokens_without_valid_expiry(self):
        for token in ("", "JWT abc", "JWT a.b.c", self.jwt({"sub": 1}), self.jwt({"exp": "x"})):
            self.assertIsNone(token_expiry(token))


if __name__ == "__main__":
    unittest.main()


class HashtagTests(unittest.TestCase):
    def test_part_is_the_first_three_letters_without_digits_or_punctuation(self):
        self.assertEqual(hashtag_part("VSCO'61"), "vsc")
        self.assertEqual(hashtag_part("1e Hattem"), "eha")  # het cijfer valt weg, de e blijft
        self.assertEqual(hashtag_part("VSW 1"), "vsw")
        self.assertEqual(hashtag_part("WSV 30"), "wsv")
        self.assertEqual(hashtag_part("Ésprit"), "ésp")  # letters met accent blijven
        self.assertEqual(hashtag_part("De Hoogt"), "deh")
        self.assertEqual(hashtag_part("AB"), "ab")
        self.assertEqual(hashtag_part("123"), "")

    def test_default_is_home_plus_away(self):
        self.assertEqual(default_hashtag("VSCO'61", "VSW 1"), "#vscvsw")
        # een ander team dan VSCO begint niet meer met vsc
        self.assertEqual(default_hashtag("Limmen", "WSV 30"), "#limwsv")
        # bij een uitwedstrijd staat de thuisploeg voorop
        self.assertEqual(default_hashtag("Hattem", "VSCO 61"), "#hatvsc")

    def test_validate_normalizes_and_rejects(self):
        self.assertEqual(validate_hashtag("vscvsw"), "#vscvsw")
        self.assertEqual(validate_hashtag("  #VSCvsw  "), "#VSCvsw")
        self.assertEqual(validate_hashtag("##dubbel"), "#dubbel")
        self.assertEqual(validate_hashtag(""), "")
        self.assertEqual(validate_hashtag("#"), "")
        self.assertEqual(validate_hashtag("derby_2026"), "#derby_2026")
        for bad in ("met spatie", "a-b", "#a#b", "x" * 31, "<script>", "a\nb"):
            with self.assertRaises(ValueError, msg=bad):
                validate_hashtag(bad)


class HashtagSettingsTests(unittest.TestCase):
    def test_override_is_saved_reloaded_and_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "s.json"
            settings = Settings(path)
            self.assertEqual(settings.hashtag_override, "")
            settings.set_hashtag("derby")
            self.assertEqual(settings.hashtag_override, "#derby")
            self.assertEqual(Settings(path).hashtag_override, "#derby")  # blijft na een herstart
            settings.clear_hashtag()
            self.assertEqual(Settings(path).hashtag_override, "")

    def test_an_invalid_value_is_refused_and_a_broken_file_value_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "s.json"
            settings = Settings(path)
            settings.set_hashtag("goed")
            with self.assertRaises(ValueError):
                settings.set_hashtag("niet goed")
            self.assertEqual(settings.hashtag_override, "#goed")  # ongewijzigd
            data = json.loads(path.read_text(encoding="utf-8"))
            data["hashtag_override"] = "kapot met spaties"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(Settings(path).hashtag_override, "")
