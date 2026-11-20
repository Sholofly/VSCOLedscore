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
        for path in ("/", "/settings", "/templates", "/logs"):
            resp = await self.client.get(path, headers=AUTH)
            body = await resp.text()
            self.assertEqual(resp.status, 200, path)
            if path == "/settings":
                self.assertIn("supergeheim", body)
            else:
                self.assertNotIn("supergeheim", body, path)
            self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])
            self.assertEqual(resp.headers["Cache-Control"], "no-store")
        status = await (await self.client.get("/", headers=AUTH)).text()
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
        other = await self.client.get("/templates", headers=AUTH)
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
        self.assertLess(len(body), 20000)

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

    async def test_every_message_gets_a_ready_to_post_link_for_x(self):
        from urllib.parse import parse_qs, urlsplit

        text = "'12 DOELPUNT VSCO'61! Naam & Co! VSCO'61 - VSW 1 1-0. #vscvsw"
        self.state.add_message(text)
        body = await (await self.client.get("/", headers=AUTH)).text()
        data = await (await self.client.get("/logs/data", headers=AUTH)).json()

        url = data["messages"][0]["url"]
        parts = urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "x.com", "/intent/post"))
        self.assertEqual(parse_qs(parts.query), {"text": [text]})  # de tekst komt ongewijzigd door
        self.assertNotIn("&Co", url)  # & en # zijn gecodeerd, dus geen extra queryparameters
        self.assertNotIn("#vsc", url)
        self.assertIn('class="tweet"', body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertIn("/intent/post?text=", body)
        self.assertEqual(data["messages"][0]["length"], len(text))

    async def test_message_longer_than_280_characters_is_flagged(self):
        self.state.add_message("x" * 281)
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
        self.assertEqual(body.count('class="share"'), 1)  # alleen bij het bericht met foto
        self.assertIn(f'src="{self.photo_path(self.PHOTO)}"', body)
        self.assertIn('data-text="&#x27;12 DOELPUNT VSCO&#x27;61! Speler A! ..."', body)
        self.assertNotIn(self.PHOTO, body)  # de browser ziet alleen ons eigen adres
        self.assertIn("img-src 'self'", resp.headers["Content-Security-Policy"])
        self.assertIn("navigator.share", body)

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
        expires, signature = good.split(".")
        self.assertFalse(valid_session(self.app, f"{int(expires) + 999999}.{signature}", now=1001))
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
