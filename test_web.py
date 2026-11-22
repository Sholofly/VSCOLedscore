import unittest
import asyncio
import tempfile
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

from aiohttp import BasicAuth
from aiohttp.test_utils import AioHTTPTestCase

from messages import DEFAULT_TEMPLATES
from settings import Settings
from state import ConnectionState, MemoryLogHandler
from web import create_web_app, display_url, tail_lines

TOKEN = "JWT supergeheim-token-123"
AUTH = {"Authorization": BasicAuth("admin", "wachtwoord").encode()}


class WebTests(AioHTTPTestCase):
    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.app_log = root / "app.log"
        self.raw_log = root / "raw.log"
        self.settings = Settings(root / "settings.json", "wss://example.test/club/", TOKEN)
        self.state = ConnectionState()
        return create_web_app(
            self.settings, self.state, self.app_log, self.raw_log, "admin", "wachtwoord"
        )

    def post_data(self, **fields):
        return {"csrf": self.app["csrf"], **fields}

    async def test_everything_except_health_requires_login(self):
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        for path in ("/", "/settings", "/templates", "/logs"):
            resp = await self.client.get(path, headers={"Accept": "text/html"}, allow_redirects=False)
            self.assertEqual(resp.status, 303, path)  # naar de inlogpagina, geen browser-popup
            location = urlsplit(resp.headers["Location"])
            self.assertEqual((location.path, parse_qs(location.query)), ("/login", {"next": [path]}))
            self.assertNotIn("WWW-Authenticate", resp.headers)
        for path in ("/logs/data", "/settings"):
            self.assertEqual((await self.client.get(path)).status, 401, path)  # geen HTML gevraagd
        self.assertEqual((await self.client.post("/settings", data={"csrf": self.app["csrf"]})).status, 401)
        wrong = {"Authorization": BasicAuth("admin", "fout").encode()}
        self.assertEqual((await self.client.get("/", headers=wrong)).status, 401)
        self.assertEqual((await self.client.get("/", headers={"Authorization": "Basic %%%"})).status, 401)

    async def test_pages_render_and_only_the_connection_page_contains_the_token(self):
        self.state.set("verbonden", "ok")
        self.state.record_error("iets misgegaan")
        for path in ("/", "/pushmeldingen", "/account", "/app"):
            resp = await self.client.get(path, headers=AUTH)
            body = await resp.text()
            self.assertEqual(resp.status, 200, path)
            if path == "/app":
                self.assertIn("supergeheim", body)
            else:
                self.assertNotIn("supergeheim", body, path)
            self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])
            self.assertEqual(resp.headers["Cache-Control"], "no-store")
        for old, target in (("/status", "/app#status"), ("/settings", "/app#scorebord"),
                            ("/templates", "/sjablonen"), ("/logs", "/app#logging")):
            resp = await self.client.get(old, headers=AUTH, allow_redirects=False)
            self.assertEqual((resp.status, resp.headers["Location"]), (303, target), old)
        status = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn("verbonden", status)
        self.assertIn("iets misgegaan", status)
        self.assertIn("ingesteld", status)

    async def test_post_without_valid_csrf_token_is_refused(self):
        for data in ({"action": "url", "url": "wss://evil.test/"}, {"csrf": "fout", "action": "url", "url": "wss://evil.test/"}):
            resp = await self.client.post("/settings", data=data, headers=AUTH)
            self.assertEqual(resp.status, 403)
        self.assertEqual(self.settings.url, "wss://example.test/club/")

    async def test_changing_url_signals_reconnect(self):
        resp = await self.client.post(
            "/settings", data=self.post_data(action="url", url="wss://nieuw.test/club2/"),
            headers=AUTH, allow_redirects=False,
        )
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.settings.url, "wss://nieuw.test/club2/")
        self.assertTrue(self.settings.connection_changed.is_set())

    async def test_home_page_shows_only_messages_with_a_status_led(self):
        self.state.set("verbonden", "ok")
        self.state.add_message("bericht")
        body = await (await self.client.get("/", headers=AUTH)).text()
        self.assertIn('class="led ok"', body)
        self.assertIn('href="/app#status"', body)  # lampje leidt (voor de beheerder) naar de status
        self.assertIn("<details class=\"intro\"><summary>", body)  # uitleg ingeklapt
        self.assertNotIn('id="live-table"', body)  # geen statustabel op de startpagina
        self.assertIn('id="live-messages"', body)
        data = await (await self.client.get("/logs/data", headers=AUTH)).json()
        self.assertEqual(data["led"], {"level": "ok", "status": "verbonden"})

    async def test_status_page_has_the_table_but_no_messages(self):
        body = await (await self.client.get("/status", headers=AUTH)).text()
        self.assertIn('id="live-table"', body)
        self.assertNotIn('id="live-messages"', body)

    async def test_raw_messages_are_shown_as_readable_json_with_a_summary(self):
        import json as _json
        message = {
            "scoreboard_data": {
                "startedAt": "x", "endedAt": None, "rest": False, "time": 74, "period": 1,
                "teams": {"home": 1, "away": 2}, "scoreboard": {"home": 2, "away": 1},
            },
            "teams": [{"id": 1, "scoreboard_name": "Home <FC>"}, {"id": 2, "scoreboard_name": "Away FC"}],
            "sponsors": ["groot"] * 50,
        }
        self.raw_log.write_text(
            "2026-11-20 21:47:12,123 " + _json.dumps(message) + "\n"
            + "2026-11-20 21:47:13,000 {kapot\n",
            encoding="utf-8",
        )
        body = await (await self.client.get("/logs", headers=AUTH)).text()
        self.assertIn("Home &lt;FC&gt; 2-1 Away FC · 2e helft 1:14", body)
        self.assertIn('&quot;period&quot;: 1,\n  &quot;rest&quot;', body)  # ingesprongen, gesorteerd
        self.assertIn("Weggelaten (staat in het logbestand): sponsors, teams.", body)
        self.assertNotIn("groot", body)  # grote velden niet op de pagina
        self.assertIn("geen geldige JSON", body)
        self.assertNotIn("<FC>", body)

    async def test_goal_wait_can_be_changed_on_the_connection_page(self):
        page = await (await self.client.get("/settings", headers=AUTH)).text()
        self.assertIn('name="goal_wait"', page)
        self.assertIn('value="20"', page)
        resp = await self.client.post(
            "/settings", data=self.post_data(action="goal_wait", goal_wait="35"), headers=AUTH
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.settings.goal_wait_seconds, 35)
        self.assertIn("Wachttijd opgeslagen", await resp.text())
        self.assertFalse(self.settings.connection_changed.is_set())

        bad = await self.client.post(
            "/settings", data=self.post_data(action="goal_wait", goal_wait="999"), headers=AUTH
        )
        body = await bad.text()
        self.assertEqual(bad.status, 400)
        self.assertIn("0 tot 300", body)
        self.assertIn('value="999"', body)  # ingevulde waarde blijft staan
        self.assertEqual(self.settings.goal_wait_seconds, 35)

    async def test_hashtag_block_explains_the_default_and_saves_an_own_hashtag(self):
        page = await (await self.client.get("/sjablonen", headers=AUTH)).text()
        self.assertIn('id="hashtag"', page)
        self.assertIn('name="hashtag"', page)
        self.assertIn("#vscvsw", page)  # het voorbeeld van de standaard
        self.assertIn("Nu is de standaard actief", page)
        resp = await self.client.post(
            "/settings", data=self.post_data(action="hashtag", hashtag=" derby "), headers=AUTH
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.settings.hashtag_override, "#derby")  # met # bewaard
        body = await resp.text()
        self.assertIn("Hashtag opgeslagen", body)
        self.assertIn("#derby", body)
        self.assertFalse(self.settings.connection_changed.is_set())  # de verbinding blijft

    async def test_a_hashtag_with_spaces_is_refused_and_the_input_is_kept(self):
        resp = await self.client.post(
            "/settings", data=self.post_data(action="hashtag", hashtag="twee woorden"), headers=AUTH
        )
        self.assertEqual(resp.status, 400)
        body = await resp.text()
        self.assertIn("zonder spaties", body)
        self.assertIn('value="twee woorden"', body)
        self.assertEqual(self.settings.hashtag_override, "")

    async def test_the_default_can_be_restored(self):
        self.settings.set_hashtag("derby")
        resp = await self.client.post(
            "/settings", data=self.post_data(action="hashtag_reset"), headers=AUTH
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.settings.hashtag_override, "")
        self.assertIn("hersteld naar de standaard", await resp.text())

    async def test_the_hashtag_is_escaped_in_the_page(self):
        self.settings.hashtag_override = "<b>x</b>"  # bv. uit een handmatig aangepast bestand
        page = await (await self.client.get("/sjablonen", headers=AUTH)).text()
        self.assertNotIn("<b>x</b>", page)

    async def test_invalid_url_is_refused_with_message(self):
        resp = await self.client.post(
            "/settings", data=self.post_data(action="url", url="http://nope.test"), headers=AUTH
        )
        self.assertEqual(resp.status, 400)
        self.assertIn("ws://", await resp.text())
        self.assertEqual(self.settings.url, "wss://example.test/club/")

    async def test_token_is_hidden_by_default_with_a_working_eye_toggle(self):
        resp = await self.client.get("/settings", headers=AUTH)
        body = await resp.text()
        self.assertIn('type="password" id="token"', body)
        self.assertIn(f'value="{TOKEN}"', body)
        self.assertIn('class="reveal"', body)
        self.assertIn('aria-label="Toon token"', body)
        # Het script mag alleen met de nonce van deze pagina draaien.
        nonce = resp.headers["Content-Security-Policy"].split("script-src 'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">', body)
        script_src = [
            part for part in resp.headers["Content-Security-Policy"].split(";")
            if part.strip().startswith("script-src")
        ][0]
        self.assertNotIn("unsafe", script_src)
        other = await self.client.get("/account", headers=AUTH)
        self.assertNotIn("querySelectorAll('.reveal')", await other.text())  # oog-script alleen op Verbinding

    async def test_token_with_markup_is_escaped_in_the_form(self):
        self.settings.token = 'JWT "><script>alert(1)</script>'
        body = await (await self.client.get("/settings", headers=AUTH)).text()
        self.assertNotIn("<script>alert(1)", body)
        self.assertIn("&lt;script&gt;alert(1)", body)

    async def test_changing_token_saves_it_and_shows_the_new_one_on_the_page(self):
        new = "JWT nieuw-geheim-456"
        resp = await self.client.post(
            "/settings", data=self.post_data(action="token", token=new), headers=AUTH
        )
        self.assertEqual(resp.status, 200)  # redirect gevolgd naar /settings
        self.assertEqual(self.settings.token, new)
        self.assertIn(new, await resp.text())
        self.assertTrue(self.settings.connection_changed.is_set())

        bad = await self.client.post(
            "/settings", data=self.post_data(action="token", token="   "), headers=AUTH
        )
        self.assertEqual(bad.status, 400)
        self.assertEqual(self.settings.token, new)

    async def test_resubmitting_the_unchanged_token_does_not_reconnect(self):
        await self.client.post(
            "/settings", data=self.post_data(action="token", token=TOKEN), headers=AUTH
        )
        self.assertFalse(self.settings.connection_changed.is_set())

    async def test_templates_can_be_saved_previewed_and_reset(self):
        values = {f"tpl_{key}": text for key, text in DEFAULT_TEMPLATES.items()}
        values["tpl_start"] = "Aftrap! {stand} {afsluiting}"
        resp = await self.client.post(
            "/templates", data=self.post_data(**values), headers=AUTH
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.settings.templates["start"], "Aftrap! {stand} {afsluiting}")
        page = await (await self.client.get("/templates", headers=AUTH)).text()
        self.assertIn("Aftrap! 2-1 VSCO&#x27;61 - VSW 1 2-1. #vscvsw", page)

        resp = await self.client.post("/templates/reset", data=self.post_data(), headers=AUTH)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.settings.templates, DEFAULT_TEMPLATES)

    async def test_invalid_template_is_refused_and_nothing_changes(self):
        values = {f"tpl_{key}": text for key, text in DEFAULT_TEMPLATES.items()}
        values["tpl_goal_away"] = "{speler} scoort"
        resp = await self.client.post("/templates", data=self.post_data(**values), headers=AUTH)
        body = await resp.text()
        self.assertEqual(resp.status, 400)
        self.assertIn("{speler}", body)
        self.assertEqual(self.settings.templates, DEFAULT_TEMPLATES)

    async def test_logs_are_escaped_and_long_raw_entries_truncated(self):
        self.app_log.write_text("2026 INFO <script>alert(1)</script>\n", encoding="utf-8")
        self.raw_log.write_text("2026 " + "x" * 5000 + "\n", encoding="utf-8")
        body = await (await self.client.get("/logs", headers=AUTH)).text()
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("ingekort", body)
        self.assertNotIn("x" * 2000, body)  # lange ruwe regels worden ingekort

    async def test_logs_fall_back_to_memory_when_the_log_file_is_unavailable(self):
        import logging

        memory = MemoryLogHandler()
        memory.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("test_web_memory")
        logger.propagate = False
        logger.addHandler(memory)
        self.addCleanup(logger.removeHandler, memory)
        logger.warning("alleen in het geheugen <b>")
        self.app["memory_log"] = memory

        body = await (await self.client.get("/logs", headers=AUTH)).text()
        self.assertIn("WARNING alleen in het geheugen &lt;b&gt;", body)
        self.assertIn("logbestand is niet beschikbaar", body)

        self.app_log.write_text("2026 INFO uit bestand\n", encoding="utf-8")
        body = await (await self.client.get("/logs", headers=AUTH)).text()
        self.assertIn("uit bestand", body)
        self.assertNotIn("alleen in het geheugen", body)

    async def test_live_data_endpoint_returns_messages_and_log_and_needs_login(self):
        self.assertEqual((await self.client.get("/logs/data")).status, 401)
        self.state.add_message("'12 DOELPUNT <VSCO>! Naam!")
        self.app_log.write_text("2026 INFO eerste\n2026 INFO tweede\n", encoding="utf-8")
        resp = await self.client.get("/logs/data", headers=AUTH)
        data = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(data["messages"][0]["text"], "'12 DOELPUNT <VSCO>! Naam!")
        self.assertIn("&lt;VSCO&gt;", data["messages_html"])  # de HTML-versie is ge-escaped
        self.assertNotIn("<VSCO>", data["messages_html"])
        self.assertTrue(data["app"].startswith("2026 INFO tweede"))  # nieuwste bovenaan
        self.assertNotIn("supergeheim", str(data))

    async def test_home_page_shows_messages_and_is_live(self):
        self.state.add_message("'5 Doelpunt <b>Uit</b>.")
        resp = await self.client.get("/", headers=AUTH)
        body = await resp.text()
        self.assertIn("&#x27;5 Doelpunt &lt;b&gt;Uit&lt;/b&gt;.", body)
        self.assertIn("/logs/data", body)
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("http-equiv", body)  # geen volledige herlaadslag meer nodig

    async def test_every_message_gets_a_share_button_with_x_as_fallback(self):
        from urllib.parse import parse_qs, urlsplit

        text = "'12 DOELPUNT VSCO'61! Naam & Co! VSCO'61 - VSW 1 1-0. #vscvsw"
        self.state.add_message(text)
        body = await (await self.client.get("/", headers=AUTH)).text()
        data = await (await self.client.get("/logs/data", headers=AUTH)).json()

        url = data["messages"][0]["url"]
        parts = urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "x.com", "/intent/post"))
        # De tekst komt ongewijzigd door, met een enter aan het eind (geen hashtag-venster in X).
        self.assertEqual(parse_qs(parts.query), {"text": [text + "\n"]})
        self.assertNotIn("&Co", url)  # & en # zijn gecodeerd, dus geen extra queryparameters
        self.assertNotIn("#vsc", url)
        self.assertIn('class="share"', body)  # altijd een Delen-knop, ook zonder foto
        self.assertNotIn("Plaats op X", body)
        self.assertNotIn('class="with-photo"', body)  # geen foto, dus geen vinkje
        self.assertIn('data-fallback="https://x.com/intent/post?text=', body)  # zonder deelmenu
        self.assertEqual(data["messages"][0]["length"], len(text) + 1)  # X telt de enter mee
        self.assertEqual(data["messages"][0]["text"], text)  # het bericht zelf blijft zonder enter
        self.assertIn(' #vscvsw&#10;"', body)  # Delen: gedeelde tekst eindigt met een enter

    async def test_message_longer_than_280_characters_is_flagged(self):
        self.state.add_message("x" * 280)  # plus de enter: 281
        body = await (await self.client.get("/", headers=AUTH)).text()
        self.assertIn("281/280 - te lang voor X", body)

    async def test_no_messages_shows_a_hint_and_no_link(self):
        body = await (await self.client.get("/", headers=AUTH)).text()
        self.assertIn("nog geen wedstrijdberichten", body)
        self.assertNotIn('class="tweet"', body)

    async def test_missing_log_files_do_not_break_the_page(self):
        resp = await self.client.get("/logs", headers=AUTH)
        self.assertEqual(resp.status, 200)
        self.assertIn("nog geen logregels", await resp.text())


class PhotoTests(AioHTTPTestCase):
    PHOTO = "https://api.example.test/media/files/speler_goal.jpg"

    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.state = ConnectionState()
        self.fetched: list[str] = []

        async def fake_fetch(url):
            self.fetched.append(url)
            if url.endswith("kapot.jpg"):
                raise ValueError("geen foto")
            return b"\xff\xd8jpegdata", "image/jpeg"

        settings = Settings(root / "settings.json", "wss://example.test/", TOKEN)
        return create_web_app(
            settings, self.state, root / "app.log", root / "raw.log", "admin", "wachtwoord",
            photo_fetcher=fake_fetch,
        )

    def photo_path(self, url):
        from web import photo_id
        return f"/photo/{photo_id(url)}"

    async def test_message_with_photo_shows_thumbnail_and_share_button(self):
        self.state.add_message("'12 DOELPUNT VSCO'61! Speler A! ...", self.PHOTO)
        self.state.add_message("'30 Doelpunt VSW 1. ...")
        resp = await self.client.get("/", headers=AUTH)
        body = await resp.text()
        self.assertEqual(body.count('class="share"'), 2)  # bij elk bericht
        self.assertEqual(body.count('class="with-photo" checked'), 1)  # vinkje alleen bij de foto, standaard aan
        self.assertEqual(body.count('msg-item has-photo'), 1)
        self.assertIn(f'src="{self.photo_path(self.PHOTO)}"', body)
        self.assertIn('data-text="&#x27;12 DOELPUNT VSCO&#x27;61! Speler A! ...&#10;"', body)
        self.assertNotIn(self.PHOTO, body)  # de browser ziet alleen ons eigen adres
        self.assertIn("img-src 'self'", resp.headers["Content-Security-Policy"])
        self.assertIn("navigator.share", body)
        self.assertIn('width="72" height="72"', body)  # vaste maat: de rij verspringt niet bij het laden

    async def test_message_keys_are_stable_between_updates(self):
        self.state.add_message("doelpunt", self.PHOTO)
        first = (await (await self.client.get("/logs/data", headers=AUTH)).json())["messages"][0]["key"]
        self.state.add_message("nog een")
        data = (await (await self.client.get("/logs/data", headers=AUTH)).json())["messages"]
        self.assertEqual(data[1]["key"], first)  # het vinkje blijft bij hetzelfde bericht
        self.assertNotEqual(data[0]["key"], first)

    async def test_known_photo_is_passed_through_and_cached(self):
        self.state.add_message("doelpunt", self.PHOTO)
        for _ in range(2):
            resp = await self.client.get(self.photo_path(self.PHOTO), headers=AUTH)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.content_type, "image/jpeg")
            self.assertEqual(await resp.read(), b"\xff\xd8jpegdata")
            self.assertIn("max-age", resp.headers["Cache-Control"])
        self.assertEqual(self.fetched, [self.PHOTO])  # maar één keer opgehaald

    async def test_only_photos_from_messages_can_be_fetched(self):
        unknown = self.photo_path("https://evil.test/intern")
        self.assertEqual((await self.client.get(unknown, headers=AUTH)).status, 404)
        self.assertEqual((await self.client.get("/photo/niet-hex", headers=AUTH)).status, 404)
        self.assertEqual(self.fetched, [])

    async def test_photo_needs_login(self):
        self.state.add_message("doelpunt", self.PHOTO)
        self.assertEqual((await self.client.get(self.photo_path(self.PHOTO))).status, 401)
        self.assertEqual(self.fetched, [])

    async def test_failing_photo_gives_bad_gateway_and_is_not_cached(self):
        broken = "https://api.example.test/media/kapot.jpg"
        self.state.add_message("doelpunt", broken)
        with self.assertLogs("web", level="WARNING"):
            resp = await self.client.get(self.photo_path(broken), headers=AUTH)
        self.assertEqual(resp.status, 502)
        with self.assertLogs("web", level="WARNING"):
            await self.client.get(self.photo_path(broken), headers=AUTH)
        self.assertEqual(len(self.fetched), 2)


class FetchPhotoTests(unittest.IsolatedAsyncioTestCase):
    """fetch_photo tegen een echte (lokale) server die de foto in stukken stuurt."""

    PHOTO = b"\xff\xd8" + bytes(range(256)) * 600 + b"\xff\xd9"  # ~150 KB

    async def asyncSetUp(self):
        from aiohttp import web as aioweb
        from aiohttp.test_utils import TestServer

        async def chunked(request):
            response = aioweb.StreamResponse(headers={"Content-Type": "image/jpeg"})
            response.content_length = len(self.PHOTO)
            await response.prepare(request)
            for start in range(0, len(self.PHOTO), 8192):
                await response.write(self.PHOTO[start:start + 8192])
                await asyncio.sleep(0.001)  # zoals een echte server: niet alles tegelijk
            await response.write_eof()
            return response

        async def text(request):
            return aioweb.Response(text="geen foto")

        async def big(request):
            return aioweb.Response(body=b"x" * 2048, content_type="image/png")

        app = aioweb.Application()
        app.add_routes([aioweb.get("/foto.jpg", chunked), aioweb.get("/tekst", text), aioweb.get("/groot", big)])
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)

    async def test_whole_photo_is_read_even_when_it_arrives_in_pieces(self):
        from web import fetch_photo

        data, content_type = await fetch_photo(str(self.server.make_url("/foto.jpg")))
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(len(data), len(self.PHOTO))
        self.assertEqual(data, self.PHOTO)

    async def test_non_images_and_too_large_photos_are_refused(self):
        import web

        with self.assertRaises(ValueError):
            await web.fetch_photo(str(self.server.make_url("/tekst")))
        original = web.MAX_PHOTO_BYTES
        web.MAX_PHOTO_BYTES = 1024
        self.addCleanup(setattr, web, "MAX_PHOTO_BYTES", original)
        with self.assertRaises(ValueError):
            await web.fetch_photo(str(self.server.make_url("/groot")))


class SessionTests(AioHTTPTestCase):
    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.secret = b"s" * 32
        self.settings = Settings(self.root / "settings.json", "wss://example.test/", TOKEN)
        return create_web_app(
            self.settings, ConnectionState(), self.root / "app.log", self.root / "raw.log",
            "admin", "wachtwoord", session_secret=self.secret,
        )

    def login(self, password="wachtwoord", next_url="/", headers=None):
        data = {"csrf": self.app["csrf"], "username": "admin", "password": password, "next": next_url}
        return self.client.post("/login", data=data, headers=headers, allow_redirects=False)

    async def test_login_page_is_public_and_has_no_navigation(self):
        resp = await self.client.get("/login")
        body = await resp.text()
        self.assertEqual(resp.status, 200)
        self.assertIn('name="password"', body)
        self.assertIn("30 dagen", body)
        self.assertNotIn("Uitloggen", body)

    async def test_wrong_password_sets_no_cookie(self):
        resp = await self.login("fout")
        self.assertEqual(resp.status, 401)
        self.assertNotIn("Set-Cookie", resp.headers)
        self.assertIn("Onjuiste", await resp.text())

    async def test_login_sets_a_long_lived_safe_cookie_and_then_pages_work_without_basic_auth(self):
        resp = await self.login(next_url="/logs")
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.headers["Location"], "/logs")
        cookie = resp.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn(f"Max-Age={30 * 86400}", cookie)
        self.assertNotIn("Secure", cookie)  # gewone http: anders slaat de browser hem niet op
        self.assertNotIn("wachtwoord", cookie)
        for path in ("/", "/settings", "/templates", "/logs", "/logs/data"):
            self.assertEqual((await self.client.get(path)).status, 200, path)

    async def test_cookie_is_secure_behind_an_https_proxy(self):
        resp = await self.login(headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", resp.headers["Set-Cookie"])

    async def test_login_only_redirects_within_the_app(self):
        for bad in ("//evil.test/", "https://evil.test/", "evil", "/\\evil.test"):
            resp = await self.login(next_url=bad)
            self.assertEqual(resp.headers["Location"], "/", bad)

    async def test_login_without_csrf_is_refused(self):
        resp = await self.client.post(
            "/login", data={"username": "admin", "password": "wachtwoord"}, allow_redirects=False
        )
        self.assertEqual(resp.status, 403)

    async def test_logout_removes_the_session(self):
        await self.login()
        self.assertEqual((await self.client.get("/")).status, 200)
        resp = await self.client.post("/logout", data={"csrf": self.app["csrf"]}, allow_redirects=False)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.headers["Location"], "/login")
        resp = await self.client.get("/", headers={"Accept": "text/html"}, allow_redirects=False)
        self.assertEqual(resp.status, 303)

    async def test_tampered_expired_or_old_password_sessions_are_rejected(self):
        from web import make_session, valid_session

        good = make_session(self.app, now=1000)
        self.assertTrue(valid_session(self.app, good, now=1001))
        self.assertFalse(valid_session(self.app, good, now=1000 + 30 * 86400 + 1))  # verlopen
        payload, signature = good.rsplit(".", 1)
        role, expires = payload.split(":")
        self.assertFalse(valid_session(self.app, f"{role}:{int(expires) + 999999}.{signature}", now=1001))
        self.assertFalse(valid_session(self.app, f"user:{expires}.{signature}", now=1001))  # andere rol
        self.assertFalse(valid_session(self.app, f"{expires}.{signature}", now=1001))  # oud formaat
        for broken in ("", "abc", ".", "x.y", None):
            self.assertFalse(valid_session(self.app, broken))
        other = {**self.app, "password": "nieuw-wachtwoord"}  # wachtwoord gewijzigd
        self.assertFalse(valid_session(other, good, now=1001))
        restarted = {**self.app, "csrf": "anders"}  # zelfde sleutel na een herstart
        self.assertTrue(valid_session(restarted, good, now=1001))

    async def test_session_secret_is_created_once_and_reused(self):
        from web import load_session_secret

        path = self.root / "data" / "session_secret"
        first = load_session_secret(path)
        self.assertEqual(len(first), 32)
        self.assertEqual(load_session_secret(path), first)
        path.write_text("kapot", encoding="utf-8")
        self.assertNotEqual(load_session_secret(path), first)  # onleesbaar: nieuwe sleutel


class RoleTests(AioHTTPTestCase):
    """Beheerder ziet alles; gebruiker alleen Berichten en Mijn instellingen."""

    USER = {"Authorization": BasicAuth("deler", "deelwachtwoord").encode()}

    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.state = ConnectionState()
        self.settings = Settings(root / "settings.json", "wss://example.test/club/", TOKEN)
        (root / "app.log").write_text("2026 INFO interne logregel\n", encoding="utf-8")
        return create_web_app(
            self.settings, self.state, root / "app.log", root / "raw.log", "admin", "wachtwoord",
            share_user="deler", share_password="deelwachtwoord",
        )

    async def login(self, username, password):
        data = {"csrf": self.app["csrf"], "username": username, "password": password, "next": "/"}
        return await self.client.post("/login", data=data, allow_redirects=False)

    async def test_user_sees_only_messages_and_own_settings(self):
        resp = await self.login("deler", "deelwachtwoord")
        self.assertEqual(resp.status, 303)
        home = await (await self.client.get("/")).text()
        # Accountmenu (hamburger) met Pushmeldingen en Account, los van het hoofdmenu.
        self.assertIn('<details class="app-menu" id="menu">', home)
        self.assertIn('href="/pushmeldingen">Pushmeldingen</a>', home)
        self.assertIn('href="/account">Account</a>', home)
        self.assertNotIn('href="/app"', home)
        self.assertNotIn('href="/app#status"', home)  # lampje is voor een gebruiker geen link
        me = await (await self.client.get("/account")).text()
        self.assertIn("Ingelogd als <strong>gebruiker</strong>", me)
        self.assertIn('class="close"', me)
        # Zonder Connectie en Sjablonen: geen scheiding boven het account.
        self.assertIn('<nav class="menu-list" aria-label="Menu"><a href="/pushmeldingen">', home)
        old = await self.client.get("/me", allow_redirects=False)
        self.assertEqual((old.status, old.headers["Location"]), (303, "/pushmeldingen"))
        self.assertNotIn("supergeheim", home + me)

    async def test_theme_follows_the_system_until_a_user_chooses_one(self):
        await self.login("deler", "deelwachtwoord")  # ook een gebruiker mag dit kiezen
        home = await (await self.client.get("/")).text()
        self.assertIn('<html lang="nl"><head>', home)  # Systeem: geen vaste keuze
        self.assertIn("@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14171b", home)
        self.assertIn(":root[data-theme=dark]{--bg:#14171b", home)
        account = await (await self.client.get("/account")).text()
        self.assertIn('name="theme" value="system" checked>Systeem', account)
        for theme in ("dark", "light"):
            resp = await self.client.post("/theme", data={"csrf": self.app["csrf"], "theme": theme},
                                          allow_redirects=False)
            self.assertEqual((resp.status, resp.headers["Location"]), (303, "/account?msg=theme"))
            page = await (await self.client.get("/account?msg=theme")).text()
            self.assertIn(f'<html lang="nl" data-theme="{theme}">', page)
            self.assertIn(f'name="theme" value="{theme}" checked>', page)
            self.assertIn("Weergave opgeslagen", page)
        await self.client.post("/theme", data={"csrf": self.app["csrf"], "theme": "system"})
        self.assertIn('<html lang="nl"><head>', await (await self.client.get("/")).text())

    async def test_theme_needs_a_valid_choice_and_csrf(self):
        await self.login("deler", "deelwachtwoord")
        bad = await self.client.post("/theme", data={"csrf": self.app["csrf"], "theme": "paars"})
        self.assertEqual(bad.status, 400)
        no_csrf = await self.client.post("/theme", data={"theme": "dark"})
        self.assertEqual(no_csrf.status, 403)
        self.assertIn('<html lang="nl"><head>', await (await self.client.get("/")).text())

    async def test_login_page_has_no_menus(self):
        body = await (await self.client.get("/login")).text()
        self.assertNotIn('id="menu"', body)
        self.assertNotIn("app-footer\"", body)
        self.assertNotIn("<nav>", body)

    async def test_user_cannot_open_or_change_app_settings(self):
        await self.login("deler", "deelwachtwoord")
        for path in ("/app", "/sjablonen", "/status", "/settings", "/templates", "/logs"):
            page = await self.client.get(path, headers={"Accept": "text/html"}, allow_redirects=False)
            self.assertEqual((page.status, page.headers.get("Location")), (303, "/"), path)
            self.assertEqual((await self.client.get(path)).status, 403, path)
        csrf = self.app["csrf"]
        attempts = (
            ("/settings", {"csrf": csrf, "action": "token", "token": "JWT gekaapt"}),
            ("/settings", {"csrf": csrf, "action": "url", "url": "wss://evil.test/"}),
            ("/settings", {"csrf": csrf, "action": "goal_wait", "goal_wait": "0"}),
            ("/settings", {"csrf": csrf, "action": "hashtag", "hashtag": "gekaapt"}),
            ("/settings", {"csrf": csrf, "action": "hashtag_reset"}),
            ("/templates", {"csrf": csrf, "tpl_start": "gekaapt"}),
            ("/templates/reset", {"csrf": csrf}),
        )
        for path, data in attempts:
            self.assertEqual((await self.client.post(path, data=data)).status, 403, path)
        self.assertEqual(self.settings.token, TOKEN)
        self.assertEqual(self.settings.url, "wss://example.test/club/")
        self.assertEqual(self.settings.goal_wait_seconds, 20)
        self.assertEqual(self.settings.hashtag_override, "")
        self.assertNotEqual(self.settings.templates["start"], "gekaapt")

    async def test_live_data_for_a_user_has_no_logs_or_status_table(self):
        self.state.add_message("bericht")
        user = await (await self.client.get("/logs/data", headers=self.USER)).json()
        self.assertEqual(sorted(user), ["led", "messages", "messages_html"])
        admin = await (await self.client.get("/logs/data", headers=AUTH)).json()
        self.assertIn("interne logregel", admin["app"])
        self.assertIn("status_html", admin)

    async def test_admin_sees_app_with_blocks_and_saving_returns_to_the_block(self):
        await self.login("admin", "wachtwoord")
        app_page = await (await self.client.get("/app")).text()
        for block in ("status", "bron", "scorebord", "logging"):
            self.assertIn(f'<details class="block" id="{block}"', app_page)
        for block in ("hashtag", "sjablonen"):
            self.assertNotIn(f'id="{block}"', app_page)
        # Hashtag en berichtteksten staan samen op de pagina Sjablonen, allebei open.
        templates = await (await self.client.get("/sjablonen")).text()
        self.assertIn("<title>Sjablonen - MatchShareNL</title>", templates)
        self.assertIn('<details class="block" id="hashtag" open><summary>Hashtag</summary>', templates)
        self.assertIn('<details class="block" id="sjablonen" open><summary>Berichtteksten</summary>', templates)
        self.assertIn('class="close"', templates)
        resp = await self.client.post(
            "/settings", data={"csrf": self.app["csrf"], "action": "hashtag", "hashtag": "derby"},
            allow_redirects=False,
        )
        self.assertEqual(resp.headers["Location"], "/sjablonen?msg=hashtag#hashtag")
        # Connectie is een paneel over Berichten: kruisje terug; de footer met het menu blijft.
        self.assertIn('<body class="sheet">', app_page)
        self.assertIn('<a href="/" class="close" aria-label="Sluiten"', app_page)
        self.assertIn('<footer class="app-footer">', app_page)
        self.assertIn('<a href="/app" class=active aria-current=page>Connectie</a>', app_page)
        # Het menu staat op Berichten (de basis): de panelen, een scheiding, dan het account.
        home = await (await self.client.get("/")).text()
        self.assertIn('<body class="base">', home)
        self.assertNotIn('class="close"', home)
        # Footer: logo links (naar Berichten), naam in het midden, menu rechts.
        self.assertIn('<footer class="app-footer"><div class="app-footer-inner"><a href="/" class="brand-logo"', home)
        self.assertIn('<img src="/static/logo.svg"', home)
        self.assertIn('<span class="brand-name">MatchShareNL</span><details class="app-menu" id="menu">', home)
        self.assertIn('<nav class="menu-list" aria-label="Menu"><a href="/app">Connectie</a>'
                      '<a href="/sjablonen">Sjablonen</a><hr>'
                      '<a href="/pushmeldingen">Pushmeldingen</a><a href="/account">Account</a></nav>', home)
        self.assertIn("<title>Connectie - MatchShareNL</title>", app_page)
        self.assertIn("gelden voor iedereen", app_page)
        resp = await self.client.post(
            "/settings", data={"csrf": self.app["csrf"], "action": "goal_wait", "goal_wait": "30"},
            allow_redirects=False,
        )
        self.assertEqual(resp.headers["Location"], "/app?msg=goal_wait#scorebord")
        after = await (await self.client.get("/app?msg=goal_wait")).text()
        self.assertIn('<details class="block" id="scorebord" open>', after)
        self.assertIn("Wachttijd opgeslagen", after)
        bad = await self.client.post(
            "/settings", data={"csrf": self.app["csrf"], "action": "url", "url": "http://x"}
        )
        body = await bad.text()
        self.assertEqual(bad.status, 400)
        self.assertIn('<details class="block" id="scorebord" open>', body)

    async def test_user_login_is_off_without_share_password(self):
        self.app["share_password"] = ""
        resp = await self.login("deler", "")
        self.assertEqual(resp.status, 401)
        self.assertEqual((await self.client.get("/", headers=self.USER)).status, 401)

    async def test_changing_a_password_only_logs_out_that_role(self):
        from web import make_session, session_role

        admin = make_session(self.app, role="admin")
        user = make_session(self.app, role="user")
        self.assertEqual((session_role(self.app, admin), session_role(self.app, user)), ("admin", "user"))
        changed = {**self.app, "share_password": "nieuw"}
        self.assertEqual((session_role(changed, admin), session_role(changed, user)), ("admin", None))


class InstallableAppTests(AioHTTPTestCase):
    async def get_application(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        settings = Settings(root / "settings.json", "wss://example.test/", TOKEN)
        return create_web_app(settings, ConnectionState(), root / "a", root / "r", "admin", "wachtwoord")

    async def test_manifest_is_public_and_complete(self):
        resp = await self.client.get("/manifest.webmanifest")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "application/manifest+json")
        data = await resp.json(content_type=None)
        self.assertEqual((data["start_url"], data["display"]), ("/", "standalone"))
        sizes = {icon["sizes"] for icon in data["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        self.assertIn("maskable", {icon["purpose"] for icon in data["icons"]})
        for icon in data["icons"]:
            icon_resp = await self.client.get(icon["src"])
            self.assertEqual(icon_resp.status, 200, icon["src"])
            self.assertEqual((await icon_resp.read())[:8], b"\x89PNG\r\n\x1a\n")

    async def test_icons_have_the_size_from_the_manifest(self):
        data = await (await self.client.get("/manifest.webmanifest")).json(content_type=None)
        for icon in data["icons"]:
            png = await (await self.client.get(icon["src"])).read()
            width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
            self.assertEqual(f"{width}x{height}", icon["sizes"], icon["src"])
        # Het maskable icoon is een eigen, volle versie (de inhoud binnen de veilige zone).
        maskable = [icon["src"] for icon in data["icons"] if icon["purpose"] == "maskable"]
        self.assertEqual(maskable, ["/static/icon-maskable-512.png"])

    async def test_alternative_substitution_message_can_be_switched_off_and_on(self):
        self.settings = self.app["settings"]
        page = await (await self.client.get("/sjablonen", headers=AUTH)).text()
        self.assertIn('name="tpl_substitution_alt"', page)
        self.assertIn('<input type="checkbox" name="substitution_alt" value="1" checked>', page)  # standaard aan
        data = {"csrf": self.app["csrf"], "tpl_start": self.settings.templates["start"],
                "substitution_alt_shown": "1"}  # uitgevinkt: het vakje stuurt niets mee
        resp = await self.client.post("/templates", data=data, headers=AUTH, allow_redirects=False)
        self.assertEqual(resp.status, 303)
        self.assertFalse(self.settings.substitution_alt)
        self.assertFalse(Settings(self.settings.path).substitution_alt)  # bewaard
        page = await (await self.client.get("/sjablonen", headers=AUTH)).text()
        self.assertIn('<input type="checkbox" name="substitution_alt" value="1">', page)
        # Een script dat alleen sjablonen stuurt, laat het vinkje met rust.
        await self.client.post("/templates", data={"csrf": self.app["csrf"], "tpl_start": "x"}, headers=AUTH)
        self.assertFalse(self.settings.substitution_alt)
        await self.client.post("/templates", data={**data, "substitution_alt": "1"}, headers=AUTH)
        self.assertTrue(self.settings.substitution_alt)
        self.settings.substitution_alt = False
        await self.client.post("/templates/reset", data={"csrf": self.app["csrf"]}, headers=AUTH)
        self.assertTrue(self.settings.substitution_alt)  # standaard herstellen zet hem weer aan

    async def test_templates_page_has_the_substitution_template(self):
        page = await (await self.client.get("/sjablonen", headers=AUTH)).text()
        self.assertIn('name="tpl_substitution"', page)
        self.assertIn("Wissel eigen club (alleen LED Scores)", page)
        self.assertIn("{erin}", page)
        self.assertIn("{eruit}", page)

    async def test_logo_in_relief_on_the_background_for_light_and_dark(self):
        page = await (await self.client.get("/login")).text()
        self.assertIn("body::before{", page)
        self.assertIn("--watermark:url(/static/watermark-light.svg)", page)
        self.assertIn("--watermark:url(/static/watermark-dark.svg)", page)
        for name in ("watermark-light.svg", "watermark-dark.svg"):
            resp = await self.client.get(f"/static/{name}")  # openbaar, ook op de inlogpagina
            self.assertEqual((resp.status, resp.content_type), (200, "image/svg+xml"), name)
            self.assertIn("Pictogrammers, Apache 2.0", await resp.text())

    async def test_ledscore_block_shows_their_logo_from_our_own_server(self):
        page = await (await self.client.get("/app", headers=AUTH)).text()
        self.assertIn('<summary>LED Scores</summary><div class="block-body"><div class="brand-plate">'
                      '<img src="/static/ledscores-logo.png" alt="LED Scores"', page)
        self.assertNotIn("ledscores.nl/wp-content", page)  # niet extern laden (CSP en privacy)
        resp = await self.client.get("/static/ledscores-logo.png")
        self.assertEqual((resp.status, resp.content_type), (200, "image/png"))

    async def test_logo_is_an_svg_favicon(self):
        page = await (await self.client.get("/login")).text()
        self.assertIn('<link rel="icon" href="/static/logo.svg" type="image/svg+xml">', page)
        resp = await self.client.get("/static/logo.svg")
        self.assertEqual((resp.status, resp.content_type), (200, "image/svg+xml"))
        svg = await resp.text()
        self.assertIn("<title>MatchShareNL</title>", svg)
        self.assertIn("Pictogrammers, Apache 2.0", svg)  # herkomst van de MDI-iconen

    async def test_service_worker_is_public_and_stores_nothing(self):
        resp = await self.client.get("/sw.js")
        body = await resp.text()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "application/javascript")
        self.assertIn("addEventListener('fetch'", body)
        self.assertNotIn("caches.", body)  # geen pagina's met persoonlijke gegevens bewaren

    async def test_pages_link_the_manifest_and_register_the_service_worker(self):
        resp = await self.client.get("/", headers=AUTH)
        body = await resp.text()
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', body)
        self.assertIn('rel="apple-touch-icon"', body)
        self.assertIn("serviceWorker.register('/sw.js')", body)
        self.assertIn("manifest-src 'self'", csp)
        self.assertIn("worker-src 'self'", csp)

    async def test_static_files_do_not_expose_anything_else(self):
        for path in ("/static/../web.py", "/static/%2e%2e/web.py", "/static/", "/static/..%2fweb.py"):
            resp = await self.client.get(path)
            self.assertIn(resp.status, (401, 403, 404), path)
            self.assertNotIn("def create_web_app", await resp.text(), path)


class StaticTextTests(unittest.TestCase):
    def test_css_and_scripts_contain_no_control_characters(self):
        # Regressie: een CSS-escape als backslash-25B8 in een gewone Python-string werd een
        # stuurteken (octale escape), waardoor het pijltje van de uitklapper kapot was.
        import web

        for name in ("CSS", "LIVE_JS", "REVEAL_JS", "SW_REGISTER_JS", "SERVICE_WORKER_JS"):
            text = getattr(web, name)
            bad = [hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\t"]
            self.assertEqual(bad, [], name)


class ShareTextTests(unittest.TestCase):
    def test_shared_text_always_ends_with_exactly_one_enter(self):
        from web import share_text

        for text in ("bericht #vscvsw", "bericht #vscvsw\n", "bericht\n\n"):
            shared = share_text(text)
            self.assertTrue(shared.endswith("\n"), repr(text))
            self.assertFalse(shared.endswith("\n\n"), repr(text))


class HelperTests(AioHTTPTestCase):
    async def get_application(self):
        from aiohttp import web
        return web.Application()

    async def test_display_url_hides_a_token_parameter(self):
        self.assertEqual(display_url("wss://a.test/x?token=geheim&mode=live"), "wss://a.test/x?mode=live")

    async def test_tail_lines_returns_only_the_last_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log"
            path.write_text("\n".join(f"regel {i}" for i in range(1000)), encoding="utf-8")
            self.assertEqual(tail_lines(path, 2), ["regel 998", "regel 999"])
            self.assertEqual(tail_lines(path, 2, max_bytes=30)[-1], "regel 999")
            self.assertEqual(tail_lines(Path(folder) / "bestaat-niet", 5), [])
