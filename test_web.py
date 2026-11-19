import tempfile
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
            resp = await self.client.get(path)
            self.assertEqual(resp.status, 401, path)
            self.assertIn("Basic", resp.headers["WWW-Authenticate"])
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
        self.assertNotIn("<script", await other.text())

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
        self.assertIn("'12 DOELPUNT <VSCO>! Naam!", data["messages"])  # JSON, de pagina zet het als tekst neer
        self.assertTrue(data["app"].startswith("2026 INFO tweede"))  # nieuwste bovenaan
        self.assertNotIn("supergeheim", str(data))

    async def test_logs_page_shows_messages_and_is_live(self):
        self.state.add_message("'5 Doelpunt <b>Uit</b>.")
        resp = await self.client.get("/logs", headers=AUTH)
        body = await resp.text()
        self.assertIn("&#x27;5 Doelpunt &lt;b&gt;Uit&lt;/b&gt;.", body)
        self.assertIn("/logs/data", body)
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("http-equiv", body)  # geen volledige herlaadslag meer nodig

    async def test_missing_log_files_do_not_break_the_page(self):
        resp = await self.client.get("/logs", headers=AUTH)
        self.assertEqual(resp.status, 200)
        self.assertIn("nog geen logregels", await resp.text())


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
