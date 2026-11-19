"""Webomgeving: status, URL en token, berichtsjablonen en logging.

Beveiliging: alles (behalve /health) vereist inloggen met HTTP Basic, formulieren
hebben een CSRF-token, scripts zijn alleen met een nonce per pagina toegestaan en
alle uitvoer wordt ge-escaped. Het token staat alleen op de pagina Verbinding
(verborgen tot je het oog-icoon gebruikt), nooit op de andere pagina's.
"""

import asyncio
import hmac
import html
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import BasicAuth, web

from messages import (
    MAX_TEMPLATE_LENGTH,
    PLACEHOLDERS,
    SAMPLE_CONTEXT,
    TEMPLATE_INFO,
    render_message,
    validate_template,
)
from settings import Settings, token_expiry
from state import ConnectionState, MemoryLogHandler

logger = logging.getLogger(__name__)

APP_LOG_LINES = 300
RAW_LOG_ENTRIES = 10
RAW_ENTRY_CHARS = 1500
MESSAGES = {
    "url": "URL opgeslagen. De verbinding wordt opnieuw opgebouwd.",
    "token": "Token opgeslagen. De verbinding wordt opnieuw opgebouwd.",
    "templates": "Sjablonen opgeslagen.",
    "reset": "Sjablonen hersteld naar de standaardteksten.",
}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5b6670;--line:#d9dee4;
--accent:#1f5fbf;--ok:#1a7f37;--warn:#9a6700;--err:#c4302b;--code:#eef1f4}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1d2127;--text:#e6e9ed;
--muted:#9aa5b1;--line:#2f353d;--accent:#6ea8ff;--ok:#4cc26a;--warn:#e0b341;
--err:#ff7b72;--code:#262b32}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line)}
nav{max-width:60rem;margin:0 auto;padding:0 16px;display:flex;gap:4px;flex-wrap:wrap}
nav a{padding:14px 12px;color:var(--muted);text-decoration:none;border-bottom:3px solid transparent}
nav a.active{color:var(--text);border-color:var(--accent)}
main{max-width:60rem;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:8px 0 16px}h2{font-size:1.1rem;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{width:14rem;color:var(--muted);font-weight:500}
label{display:block;font-weight:600;margin:12px 0 4px}
input[type=text],input[type=password],input[type=url]{width:100%;padding:8px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--text);font:inherit}
button{padding:8px 16px;border:0;border-radius:6px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}
button.secondary{background:transparent;color:var(--text);border:1px solid var(--line)}
.msg{padding:10px 14px;border-radius:6px;margin-bottom:16px;border:1px solid var(--line)}
.msg.ok{border-color:var(--ok);color:var(--ok)}.msg.error{border-color:var(--err);color:var(--err)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;border:1px solid currentColor}
.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.pill.error{color:var(--err)}
.muted{color:var(--muted)}.small{font-size:.9rem}
pre,code{background:var(--code);border-radius:4px;font:13px/1.45 ui-monospace,monospace}
pre{padding:10px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:0}code{padding:1px 5px}
.preview{margin:4px 0 0;color:var(--muted)}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.fielderr{color:var(--err);margin-top:4px}
.pw{position:relative}.pw input{padding-right:48px}
.reveal{position:absolute;right:4px;top:50%;transform:translateY(-50%);background:transparent;
border:0;color:var(--muted);padding:8px;line-height:0;border-radius:6px}
.reveal:hover,.reveal:focus-visible{color:var(--text);outline:2px solid var(--accent)}
.reveal .eye-closed,.reveal.on .eye-open{display:none}.reveal.on .eye-closed{display:inline}
@media (max-width:600px){th{width:auto;display:block;border:0;padding-bottom:0}td{display:block}}
"""

REVEAL_JS = (
    "document.querySelectorAll('.reveal').forEach(function(b){b.addEventListener('click',function(){"
    "var i=document.getElementById(b.dataset.target);var show=i.type==='password';"
    "i.type=show?'text':'password';b.classList.toggle('on',show);"
    "b.setAttribute('aria-pressed',show);var t=show?'Verberg token':'Toon token';b.title=t;b.setAttribute('aria-label',t);});});"
)
LIVE_JS = (
    "(function(){var m=document.getElementById('live-messages'),l=document.getElementById('live-log'),"
    "n=document.getElementById('live-note'),s=document.getElementById('live-status');"
    "function tick(){fetch('/logs/data',{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.json();})"
    ".then(function(d){m.textContent=d.messages;l.textContent=d.app;n.textContent=d.note;"
    "s.textContent='Live, bijgewerkt '+new Date().toLocaleTimeString();s.className='pill ok';})"
    ".catch(function(){s.textContent='Verbinding met de pagina verloren';s.className='pill error';});}"
    "tick();setInterval(tick,2000);})();"
)
EYE_ICONS = (
    '<svg class="eye-open" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
    '<svg class="eye-closed" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M17.94 17.94A10.9 10.9 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94"/>'
    '<path d="M9.9 4.24A9.1 9.1 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>'
    '<path d="M14.12 14.12A3 3 0 1 1 9.88 9.88"/><path d="M1 1l22 22"/></svg>'
)

NAV = (("/", "Status"), ("/settings", "Verbinding"), ("/templates", "Berichten"), ("/logs", "Logging"))


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def display_url(url: str) -> str:
    """Toon de URL zonder eventuele token-queryparameter."""
    try:
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "token"]
        return urlunsplit(parts._replace(query=urlencode(query)))
    except ValueError:
        return "(ongeldige URL)"


def fmt_time(moment: datetime | None) -> str:
    return "-" if moment is None else moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def tail_lines(path: Path, max_lines: int, max_bytes: int = 1_000_000) -> list[str]:
    """Laatste regels van een bestand, zonder het hele bestand te lezen."""
    try:
        with open(path, "rb") as file:
            size = file.seek(0, 2)
            start = max(0, size - max_bytes)
            file.seek(start)
            data = file.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # eerste regel is waarschijnlijk afgebroken
    return lines[-max_lines:]


def page(
    request: web.Request, title: str, active: str, body: str, refresh: int = 0, script: str = ""
) -> web.Response:
    links = "".join(
        f'<a href="{path}"{" class=active" if path == active else ""}>{label}</a>'
        for path, label in NAV
    )
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    script_tag = f'<script nonce="{esc(request["nonce"])}">{script}</script>' if script else ""
    document = (
        '<!doctype html><html lang="nl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{meta}<title>{esc(title)} - VSCO scorebord</title><style>{CSS}</style></head>"
        f"<body><header><nav>{links}</nav></header><main><h1>{esc(title)}</h1>{body}</main>{script_tag}</body></html>"
    )
    return web.Response(text=document, content_type="text/html")


def message_box(text: str, kind: str = "ok") -> str:
    return f'<div class="msg {kind}" role="status">{esc(text)}</div>' if text else ""


def csrf_field(request: web.Request) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(request.app["csrf"])}">'


def flash(request: web.Request) -> str:
    return message_box(MESSAGES.get(request.query.get("msg", ""), ""))


# --- pagina's ---------------------------------------------------------------


async def status_page(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    state: ConnectionState = request.app["state"]
    expiry = token_expiry(settings.token) if settings.token else None
    if not settings.token:
        token_text = "niet ingesteld"
    elif expiry is None:
        token_text = "ingesteld (vervaldatum onbekend)"
    else:
        days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
        when = fmt_time(expiry)
        token_text = (
            f"ingesteld, verlopen op {when}" if days < 0 else f"ingesteld, verloopt op {when} (over {days:.0f} dagen)"
        )
    rows = [
        ("Status", f'<span class="pill {esc(state.level)}">{esc(state.status)}</span>'),
        ("Sinds", esc(fmt_time(state.since))),
        ("Laatste bericht", esc(fmt_time(state.last_message_at))),
        ("Berichten ontvangen", esc(state.messages_received)),
        ("Verbindingspogingen", esc(state.attempt)),
        ("Volgende poging", esc(fmt_time(state.next_retry_at))),
        ("Laatste fout", esc(state.last_error or "-") + (f' <span class="muted">({esc(fmt_time(state.last_error_at))})</span>' if state.last_error else "")),
        ("URL", f"<code>{esc(display_url(settings.url) or '-')}</code>"),
        ("Token", esc(token_text)),
    ]
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    body = f'<div class="card"><table>{table}</table></div>'
    return page(request, "Status", "/", body, refresh=10)


def settings_form(request: web.Request, url_error: str = "", token_error: str = "", url_value: str | None = None) -> str:
    settings: Settings = request.app["settings"]
    shown = display_url(settings.url) if url_value is None else url_value
    return (
        f"{flash(request)}"
        '<div class="card"><h2>URL</h2><form method="post" action="/settings">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="url">'
        '<label for="url">Websocket-URL (ws:// of wss://, zonder token)</label>'
        f'<input type="url" id="url" name="url" value="{esc(shown)}" required maxlength="2048" autocomplete="off">'
        f'{message_box(url_error, "error")}'
        '<div class="row"><button type="submit">URL opslaan</button></div></form></div>'
        '<div class="card"><h2>Token</h2>'
        '<form method="post" action="/settings">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="token">'
        '<label for="token">Token</label>'
        '<div class="pw"><input type="password" id="token" name="token" '
        f'value="{esc(settings.token)}" required maxlength="4096" autocomplete="off" spellcheck="false">'
        '<button type="button" class="reveal" data-target="token" title="Toon token" '
        f'aria-label="Toon token" aria-pressed="false">{EYE_ICONS}</button></div>'
        f'{message_box(token_error, "error")}'
        '<div class="row"><button type="submit">Token opslaan</button></div></form></div>'
    )


async def settings_page(request: web.Request) -> web.Response:
    return page(request, "Verbinding", "/settings", settings_form(request), script=REVEAL_JS)


async def settings_post(request: web.Request) -> web.Response:
    form = request["form"]
    settings: Settings = request.app["settings"]
    action = form.get("action")
    try:
        if action == "url":
            settings.set_url(str(form.get("url", "")))
            logger.info("URL gewijzigd via de webomgeving")
        elif action == "token":
            settings.set_token(str(form.get("token", "")))
            logger.info("Token gewijzigd via de webomgeving")
        else:
            raise ValueError("Onbekende actie.")
    except ValueError as error:
        kwargs = {"token_error": str(error)} if action == "token" else {"url_error": str(error)}
        if action == "url":
            kwargs["url_value"] = str(form.get("url", ""))
        response = page(
            request, "Verbinding", "/settings", settings_form(request, **kwargs), script=REVEAL_JS
        )
        response.set_status(400)
        return response
    raise web.HTTPSeeOther(f"/settings?msg={action}")


def templates_form(request: web.Request, values: dict[str, str], errors: dict[str, str]) -> str:
    legend = "".join(
        f"<tr><th><code>{{{esc(name)}}}</code></th><td>{esc(text)}</td></tr>"
        for name, text in PLACEHOLDERS.items()
    )
    fields = []
    for key, (label, _) in TEMPLATE_INFO.items():
        context = dict(SAMPLE_CONTEXT)
        if key.startswith("end"):
            context["minuut"] = "90 + 2"
        preview = render_message(values, key, context)
        error = f'<div class="fielderr">{esc(errors[key])}</div>' if key in errors else ""
        fields.append(
            f'<label for="tpl_{esc(key)}">{esc(label)}</label>'
            f'<input type="text" id="tpl_{esc(key)}" name="tpl_{esc(key)}" value="{esc(values[key])}" '
            f'maxlength="{MAX_TEMPLATE_LENGTH}" autocomplete="off">{error}'
            f'<div class="preview small">Voorbeeld: {esc(preview) if preview else "(geen bericht)"}</div>'
        )
    return (
        f"{flash(request)}"
        '<div class="card"><h2>Aanduidingen</h2>'
        '<p class="muted small">Gebruik deze in een sjabloon, bijvoorbeeld <code>{thuis} - {uit} {stand}</code>. '
        "Een leeg sjabloon betekent dat dat bericht niet wordt verstuurd.</p>"
        f"<table>{legend}</table></div>"
        '<div class="card"><form method="post" action="/templates">'
        f'{csrf_field(request)}{"".join(fields)}'
        '<div class="row"><button type="submit">Sjablonen opslaan</button></div></form>'
        '<form method="post" action="/templates/reset" class="row">'
        f'{csrf_field(request)}<button type="submit" class="secondary">Standaardteksten herstellen</button></form></div>'
    )


async def templates_page(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    return page(request, "Berichten", "/templates", templates_form(request, settings.templates, {}))


async def templates_post(request: web.Request) -> web.Response:
    form = request["form"]
    settings: Settings = request.app["settings"]
    submitted = {
        key: str(form.get(f"tpl_{key}", settings.templates[key])).strip() for key in TEMPLATE_INFO
    }
    errors = {key: error for key in TEMPLATE_INFO if (error := validate_template(key, submitted[key]))}
    if errors:
        response = page(request, "Berichten", "/templates", templates_form(request, submitted, errors))
        response.set_status(400)
        return response
    settings.set_templates(submitted)
    logger.info("Berichtsjablonen gewijzigd via de webomgeving")
    raise web.HTTPSeeOther("/templates?msg=templates")


async def templates_reset(request: web.Request) -> web.Response:
    request.app["settings"].reset_templates()
    logger.info("Berichtsjablonen hersteld naar de standaard via de webomgeving")
    raise web.HTTPSeeOther("/templates?msg=reset")


def live_logs(app: web.Application) -> dict[str, str]:
    """De teksten voor de pagina Logging; ook gebruikt voor de live-updates."""
    app_lines = tail_lines(app["app_log"], APP_LOG_LINES)
    note = "Nieuwste bovenaan."
    memory: MemoryLogHandler | None = app["memory_log"]
    if not app_lines and memory is not None and memory.lines:
        app_lines = list(memory.lines)[-APP_LOG_LINES:]
        note = (
            "Het logbestand is niet beschikbaar (is de logmap schrijfbaar?); dit zijn de regels "
            "sinds de laatste start. " + note
        )
    messages = [
        f"{moment.astimezone().strftime('%H:%M:%S')}  {text}"
        for moment, text in reversed(app["state"].messages)
    ]
    return {
        "messages": "\n".join(messages) or "(nog geen wedstrijdberichten)",
        "app": "\n".join(reversed(app_lines)) or "(nog geen logregels)",
        "note": note,
    }


async def logs_data(request: web.Request) -> web.Response:
    return web.json_response(live_logs(request.app))


async def logs_page(request: web.Request) -> web.Response:
    live = live_logs(request.app)
    raw_lines = tail_lines(request.app["raw_log"], RAW_LOG_ENTRIES, max_bytes=3_000_000)
    raw_entries = [
        line if len(line) <= RAW_ENTRY_CHARS else f"{line[:RAW_ENTRY_CHARS]} ... [{len(line)} tekens, ingekort]"
        for line in reversed(raw_lines)
    ]
    raw_text = "\n\n".join(raw_entries) or (
        "(geen ruwe berichten; staat het logbestand niet op schijf, dan is de logmap niet schrijfbaar)"
    )
    body = (
        '<p><span id="live-status" class="pill warn">Live-weergave starten...</span> '
        '<span class="muted small">Werkt vanzelf bij; zonder JavaScript herlaad je de pagina zelf.</span></p>'
        '<div class="card"><h2>Wedstrijdberichten</h2>'
        '<p class="muted small">De berichten die de app heeft gemaakt, sinds de laatste start. Nieuwste bovenaan.</p>'
        f'<pre id="live-messages">{esc(live["messages"])}</pre></div>'
        '<div class="card"><h2>Berichten en fouten</h2>'
        f'<p id="live-note" class="muted small">{esc(live["note"])}</p>'
        f'<pre id="live-log">{esc(live["app"])}</pre></div>'
        '<div class="card"><h2>Ruwe websocketberichten</h2>'
        f'<p class="muted small">De laatste {RAW_LOG_ENTRIES}, ingekort, bij het laden van de pagina. '
        "Het volledige bestand staat in de logmap.</p>"
        f"<pre>{esc(raw_text)}</pre></div>"
    )
    return page(request, "Logging", "/logs", body, script=LIVE_JS)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "connection": request.app["state"].status})


# --- beveiliging ------------------------------------------------------------


def is_authorized(request: web.Request, username: str, password: str) -> bool:
    header = request.headers.get("Authorization", "")
    try:
        auth = BasicAuth.decode(header, encoding="utf-8")
    except ValueError:
        return False
    user_ok = hmac.compare_digest(auth.login.encode(), username.encode())
    pass_ok = hmac.compare_digest(auth.password.encode(), password.encode())
    return user_ok and pass_ok


@web.middleware
async def security(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    app = request.app
    request["nonce"] = secrets.token_urlsafe(16)
    if not is_authorized(request, app["username"], app["password"]):
        await asyncio.sleep(1)  # remt het raden van wachtwoorden
        return web.Response(
            status=401,
            text="Inloggen vereist",
            headers={"WWW-Authenticate": 'Basic realm="VSCO scorebord", charset="UTF-8"'},
        )
    if request.method == "POST":
        form = await request.post()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), app["csrf"].encode()):
            return web.Response(status=403, text="Ongeldig formulier; laad de pagina opnieuw.")
        request["form"] = form
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{request['nonce']}'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    if isinstance(response, web.HTTPException):
        raise response
    return response


def create_web_app(
    settings: Settings,
    state: ConnectionState,
    app_log: Path,
    raw_log: Path,
    username: str,
    password: str,
    memory_log: MemoryLogHandler | None = None,
) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app["settings"] = settings
    app["state"] = state
    app["app_log"] = app_log
    app["raw_log"] = raw_log
    app["memory_log"] = memory_log
    app["username"] = username
    app["password"] = password
    app["csrf"] = secrets.token_urlsafe(32)
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/", status_page),
            web.get("/settings", settings_page),
            web.post("/settings", settings_post),
            web.get("/templates", templates_page),
            web.post("/templates", templates_post),
            web.post("/templates/reset", templates_reset),
            web.get("/logs", logs_page),
            web.get("/logs/data", logs_data),
        ]
    )
    return app


async def start_web(
    settings: Settings,
    state: ConnectionState,
    app_log: Path,
    raw_log: Path,
    username: str,
    password: str,
    host: str,
    port: int,
    memory_log: MemoryLogHandler | None = None,
) -> web.AppRunner:
    app = create_web_app(settings, state, app_log, raw_log, username, password, memory_log)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Webomgeving bereikbaar op http://%s:%s", host, port)
    return runner
