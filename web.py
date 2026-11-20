"""Webomgeving: status en berichten, URL en token, berichtsjablonen en logging.

Beveiliging: alles behalve de inlogpagina, /health en de app-bestanden (manifest,
service worker, iconen) vereist inloggen. Dat gaat via de inlogpagina met een
ondertekend sessiecookie (HttpOnly, SameSite=Lax, standaard 30 dagen), of voor
scripts via HTTP Basic. Formulieren hebben een CSRF-token, scripts zijn alleen
met een nonce per pagina toegestaan en alle uitvoer wordt ge-escaped. Het token
staat alleen op de pagina Verbinding (verborgen tot je het oog-icoon gebruikt).
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from aiohttp import BasicAuth, ClientSession, ClientTimeout, web

from messages import (
    MAX_TEMPLATE_LENGTH,
    PLACEHOLDERS,
    SAMPLE_CONTEXT,
    TEMPLATE_INFO,
    render_message,
    validate_template,
)
from settings import MAX_GOAL_WAIT_SECONDS, Settings, token_expiry
from state import ConnectionState, MatchMessage, MemoryLogHandler

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
SESSION_COOKIE = "vsco_session"
DEFAULT_SESSION_DAYS = 30
PUBLIC_PATHS = {"/health", "/login", "/manifest.webmanifest", "/sw.js"}
THEME_COLOR = "#1f5fbf"
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_CACHE_SIZE = 30
POST_URL = "https://x.com/intent/post?text="
TWEET_LIMIT = 280
APP_LOG_LINES = 300
RAW_LOG_ENTRIES = 10
RAW_ENTRY_CHARS = 1500
MESSAGES = {
    "url": "URL opgeslagen. De verbinding wordt opnieuw opgebouwd.",
    "token": "Token opgeslagen. De verbinding wordt opnieuw opgebouwd.",
    "templates": "Sjablonen opgeslagen.",
    "reset": "Sjablonen hersteld naar de standaardteksten.",
    "goal_wait": "Wachttijd opgeslagen. Hij geldt vanaf het volgende doelpunt.",
}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5b6670;--line:#d9dee4;
--accent:#1f5fbf;--on-accent:#fff;--ok:#1a7f37;--warn:#9a6700;--err:#c4302b;--code:#eef1f4}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1d2127;--text:#e6e9ed;
--muted:#9aa5b1;--line:#2f353d;--accent:#6ea8ff;--on-accent:#0b1526;--ok:#4cc26a;--warn:#e0b341;
--err:#ff7b72;--code:#262b32}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line)}
nav{max-width:60rem;margin:0 auto;padding:0 8px;display:flex;gap:0;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}nav::-webkit-scrollbar{display:none}
nav a{padding:14px 9px;color:var(--muted);text-decoration:none;border-bottom:3px solid transparent;white-space:nowrap;font-size:.95rem}
nav a.active{color:var(--text);border-color:var(--accent)}
.logout{margin-top:4px}
.login{max-width:24rem}
.titlebar{display:flex;align-items:center;justify-content:space-between;gap:8px 12px;flex-wrap:wrap;margin:8px 0 12px}
.titlebar h1{margin:0}
.led-bar{display:inline-flex;align-items:center;gap:8px;min-height:36px;padding:4px 12px;border:1px solid var(--line);
border-radius:99px;background:var(--card);color:var(--text);text-decoration:none;font-size:.9rem}
.led{width:12px;height:12px;border-radius:50%;background:var(--muted);flex:none}
.led.ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}.led.warn{background:var(--warn);box-shadow:0 0 8px var(--warn)}
.led.error{background:var(--err);box-shadow:0 0 8px var(--err)}.led.off{background:var(--muted);box-shadow:none}
details.intro{margin:0 0 12px;color:var(--muted);font-size:.92rem}
details.intro summary{cursor:pointer;min-height:32px;display:flex;align-items:center;gap:6px;list-style:none}details.intro summary::-webkit-details-marker{display:none}details.intro summary::before{content:"";width:6px;height:6px;margin:0 4px 0 2px;border-right:2px solid currentColor;
border-bottom:2px solid currentColor;transform:rotate(-45deg);transition:transform .15s}
details.intro[open] summary::before{transform:rotate(45deg)}
details.intro p{margin:4px 0 0}
details.raw{border-bottom:1px solid var(--line);padding:8px 0}details.raw:last-child{border-bottom:0}
details.raw summary{cursor:pointer;word-break:break-word}details.raw pre{margin-top:8px;max-height:60vh}input.num{max-width:8rem}
main{max-width:60rem;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:8px 0 16px}h2{font-size:1.1rem;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{width:14rem;color:var(--muted);font-weight:500}
label{display:block;font-weight:600;margin:12px 0 4px}
input[type=text],input[type=password],input[type=url],input[type=number]{width:100%;padding:8px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--text);font:inherit}
button{padding:8px 16px;border:0;border-radius:6px;background:var(--accent);color:var(--on-accent);font:inherit;cursor:pointer}
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
.msgs{list-style:none;margin:0;padding:0}
.msg-item{display:grid;grid-template-columns:1fr;gap:10px 12px;padding:14px 0;border-bottom:1px solid var(--line)}
.msg-item:first-child{padding-top:4px}.msg-item:last-child{border-bottom:0;padding-bottom:0}
.msg-item.has-photo{grid-template-columns:72px 1fr}
.thumb{width:72px;height:72px;object-fit:cover;border-radius:10px;background:var(--code);
transition:opacity .2s,filter .2s}
.msg-item:has(.with-photo:not(:checked)) .thumb{opacity:.35;filter:grayscale(1)}
.msg-body{min-width:0}
.msg-meta{display:flex;justify-content:space-between;gap:8px;font-size:.85rem;color:var(--muted)}
.msg-meta .too-long{color:var(--err);font-weight:600}
.msg-text{margin:2px 0 0;word-break:break-word;font-size:1.02rem}
.msg-actions{grid-column:1/-1;display:flex;align-items:center;gap:12px}
.msg-actions .share{flex:1;min-height:44px;display:inline-flex;align-items:center;justify-content:center;
gap:8px;font-weight:600;border-radius:10px}
.msg-actions .share svg{flex:none}
.photo-toggle{display:flex;align-items:center;gap:8px;min-height:44px;padding:0 4px;white-space:nowrap;
font-size:.95rem;cursor:pointer;-webkit-user-select:none;user-select:none}
.photo-toggle input{width:22px;height:22px;margin:0;accent-color:var(--accent)}
.item-hint{grid-column:1/-1;margin:0;color:var(--err);font-size:.9rem}.item-hint:empty{display:none}
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
LIVE_JS = """(function(){
var live=document.getElementById('live-messages'),m=live||document.getElementById('one-message'),
l=document.getElementById('live-log'),
n=document.getElementById('live-note'),dot=document.querySelector('#led .led'),lt=document.getElementById('led-text'),
t=document.getElementById('live-table'),
last=m?m.innerHTML:'',lastTable=t?t.innerHTML:'',files=new Map(),photoOff=new Set(),here=null;
if(live&&'serviceWorker' in navigator&&window.PushManager){navigator.serviceWorker.ready
.then(function(r){return r.pushManager.getSubscription();}).then(function(s){if(s)here=s.endpoint;}).catch(function(){});}
var NO_MENU='Het deelmenu werkt alleen op een telefoon of tablet via HTTPS. X is geopend met de tekst';
function prefetch(){if(!m)return;m.querySelectorAll('.msg-item[data-photo]').forEach(function(li){var u=li.dataset.photo;
if(files.has(u))return;files.set(u,null);fetch(u).then(function(r){if(!r.ok)throw 0;return r.blob();})
.then(function(x){var e=(x.type.split('/')[1]||'jpg').split('+')[0];files.set(u,new File([x],'doelpunt.'+e,{type:x.type}));})
.catch(function(){files.delete(u);});});}
function restore(){if(!m)return;m.querySelectorAll('.msg-item').forEach(function(li){
var c=li.querySelector('.with-photo');if(c)c.checked=!photoOff.has(li.dataset.key);});}
function share(li){var hint=li.querySelector('.item-hint'),c=li.querySelector('.with-photo'),
want=!!(c&&c.checked&&li.dataset.photo),f=want?files.get(li.dataset.photo):null,
data={text:li.dataset.text},note='';hint.textContent='';
if(navigator.share){
if(want&&!f){hint.textContent='De foto wordt nog geladen; probeer het zo opnieuw.';return;}
if(f){data.files=[f];if(navigator.canShare&&!navigator.canShare(data)){data={text:li.dataset.text};
note='Deze browser kan geen foto delen; alleen de tekst is gedeeld.';}}
navigator.share(data).then(function(){hint.textContent=note;})
.catch(function(e){if(e&&e.name!=='AbortError')hint.textContent='Delen mislukt: '+(e.message||e.name);});return;}
window.open(li.dataset.fallback,'_blank','noopener');
hint.textContent=NO_MENU+(want?'; voeg de foto daar zelf toe (houd de foto ingedrukt om hem op te slaan).':'.');}
if(m){m.addEventListener('click',function(ev){var b=ev.target.closest('.share');if(!b)return;
ev.preventDefault();share(b.closest('.msg-item'));});
m.addEventListener('change',function(ev){if(!ev.target.classList.contains('with-photo'))return;
var k=ev.target.closest('.msg-item').dataset.key;if(ev.target.checked)photoOff.delete(k);else photoOff.add(k);});}
var busy=false,lastOk=Date.now();
function setLed(level,text){if(dot){dot.className='led '+level;lt.textContent=text;}}
function lost(){setLed('off',navigator.onLine===false?'geen internet':'app niet bereikbaar');}
function tick(){if(busy)return;busy=true;
var ctl=window.AbortController?new AbortController():null,timer=setTimeout(function(){if(ctl)ctl.abort();},4000);
var hdr={};if(here&&!document.hidden)hdr['X-Push-Endpoint']=here;
fetch('/logs/data',{cache:'no-store',signal:ctl?ctl.signal:undefined,headers:hdr}).then(function(r){
if(r.status===401){location.href='/login?next='+encodeURIComponent(location.pathname);throw 0;}
if(!r.ok)throw 0;return r.json();})
.then(function(d){lastOk=Date.now();
if(live&&d.messages_html!==last){m.innerHTML=d.messages_html;last=d.messages_html;restore();prefetch();}
if(t&&d.status_html!==lastTable){t.innerHTML=d.status_html;lastTable=d.status_html;}
if(l)l.textContent=d.app;if(n)n.textContent=d.note;setLed(d.led.level,d.led.status);})
.catch(lost).then(function(){clearTimeout(timer);busy=false;});}
window.addEventListener('offline',lost);window.addEventListener('online',tick);
document.addEventListener('visibilitychange',function(){if(!document.hidden)tick();});
setInterval(function(){if(Date.now()-lastOk>6000)lost();},1000);
prefetch();tick();setInterval(tick,2000);})();"""
PUSH_JS = """(function(){var box=document.getElementById('push');if(!box)return;
var btn=document.getElementById('push-toggle'),test=document.getElementById('push-test'),
msg=document.getElementById('push-msg'),csrf=box.dataset.csrf;
function say(x){msg.textContent=x;}
if(!('serviceWorker' in navigator)||!window.PushManager||!window.Notification){btn.disabled=true;
say('Meldingen zijn hier niet beschikbaar. Open de app via HTTPS; op een iPhone moet hij op je beginscherm staan (Deel, Zet op beginscherm).');return;}
function key(x){x=x.replace(/-/g,'+').replace(/_/g,'/');var r=atob(x+'='.repeat((4-x.length%4)%4)),a=new Uint8Array(r.length);
for(var i=0;i<r.length;i++)a[i]=r.charCodeAt(i);return a;}
function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
body:JSON.stringify(body)}).then(function(r){if(!r.ok)throw new Error('Opslaan mislukt ('+r.status+').');return r;});}
function current(){return navigator.serviceWorker.ready.then(function(r){return r.pushManager.getSubscription();});}
function show(s){btn.textContent=s?'Meldingen uit':'Meldingen aan';btn.dataset.on=s?'1':'';test.hidden=!s;
if(!s&&Notification.permission==='denied')say('Meldingen zijn geblokkeerd. Sta ze toe in de instellingen van je telefoon of browser.');}
current().then(show).catch(function(){show(null);});
btn.addEventListener('click',function(){btn.disabled=true;say('');var job;
if(btn.dataset.on){job=current().then(function(s){if(!s)return;
return post('/push/unsubscribe',{endpoint:s.endpoint}).catch(function(){}).then(function(){return s.unsubscribe();});})
.then(function(){show(null);say('Meldingen staan uit op dit apparaat.');});}
else{job=Notification.requestPermission().then(function(p){if(p!=='granted')throw new Error('Geen toestemming voor meldingen.');
return fetch('/push/key',{cache:'no-store'}).then(function(r){if(!r.ok)throw new Error('Meldingen niet beschikbaar.');return r.json();});})
.then(function(k){return navigator.serviceWorker.ready.then(function(r){
return r.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key(k.publicKey)});});})
.then(function(s){return post('/push/subscribe',s.toJSON()).then(function(){show(s);
say('Meldingen staan aan. Je krijgt er geen als de startpagina open is.');});});}
job.catch(function(e){say(e&&e.message?e.message:String(e));}).then(function(){btn.disabled=false;});});
test.addEventListener('click',function(){say('');current().then(function(s){if(!s)return;
return post('/push/test',{endpoint:s.endpoint}).then(function(){say('Testmelding verstuurd.');});})
.catch(function(e){say(e.message);});});})();"""
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

NAV = (
    ("/", "Berichten"),
    ("/status", "Status"),
    ("/settings", "Verbinding"),
    ("/templates", "Sjablonen"),
    ("/logs", "Logging"),
)


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


SW_REGISTER_JS = (
    "if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(function(){});}"
)
APP_HEAD = (
    '<link rel="manifest" href="/manifest.webmanifest">'
    f'<meta name="theme-color" content="{THEME_COLOR}">'
    '<link rel="icon" href="/static/icon-192.png" type="image/png">'
    '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="Scorebord">'
)


def page(
    request: web.Request,
    title: str,
    active: str,
    body: str,
    refresh: int = 0,
    script: str = "",
    show_nav: bool = True,
    aside: str = "",
) -> web.Response:
    links = "".join(
        f'<a href="{path}"{" class=active" if path == active else ""}>{label}</a>'
        for path, label in NAV
    )
    nav = f"<header><nav>{links}</nav></header>" if show_nav else ""
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    nonce = esc(request["nonce"])
    script_tag = f'<script nonce="{nonce}">{SW_REGISTER_JS}{script}</script>'
    document = (
        '<!doctype html><html lang="nl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{APP_HEAD}{meta}<title>{esc(title)} - VSCO scorebord</title><style>{CSS}</style></head>"
        f'<body>{nav}<main><div class="titlebar"><h1>{esc(title)}</h1>{aside}</div>{body}</main>'
        f"{script_tag}</body></html>"
    )
    return web.Response(text=document, content_type="text/html")


def message_box(text: str, kind: str = "ok") -> str:
    return f'<div class="msg {kind}" role="status">{esc(text)}</div>' if text else ""


def csrf_field(request: web.Request) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(request.app["csrf"])}">'


def flash(request: web.Request) -> str:
    return message_box(MESSAGES.get(request.query.get("msg", ""), ""))


# --- pagina's ---------------------------------------------------------------


def status_table_html(app: web.Application) -> str:
    settings: Settings = app["settings"]
    state: ConnectionState = app["state"]
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
    return "<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def led_html(state: ConnectionState) -> str:
    """Statuslampje; tikken opent de statuspagina. Wordt live bijgewerkt."""
    return (
        '<a id="led" class="led-bar" href="/status" title="Status van de verbinding">'
        f'<span class="led {esc(state.level)}" aria-hidden="true"></span>'
        f'<span id="led-text">{esc(state.status)}</span></a>'
    )


async def messages_page(request: web.Request) -> web.Response:
    live = live_logs(request.app)
    body = (
        '<details class="intro"><summary>Hoe werkt delen?</summary>'
        "<p>De berichten sinds de laatste start, nieuwste bovenaan. Ze verschijnen vanzelf. "
        '"Delen" opent het deelmenu van je telefoon (via HTTPS), met de foto als "Foto meesturen" '
        "is aangevinkt. Zonder deelmenu, bijvoorbeeld op een pc, opent X met de tekst al ingevuld.</p></details>"
        f'<div class="card"><div id="live-messages">{live["messages_html"]}</div></div>'
    )
    return page(request, "Berichten", "/", body, script=LIVE_JS, aside=led_html(request.app["state"]))


async def status_page(request: web.Request) -> web.Response:
    live = live_logs(request.app)
    body = (
        f'<div class="card"><div id="live-table">{live["status_html"]}</div></div>'
        f"{push_card(request)}"
        '<form method="post" action="/logout" class="logout">'
        f'{csrf_field(request)}<button type="submit" class="secondary">Uitloggen</button></form>'
    )
    return page(
        request, "Status", "/status", body, script=LIVE_JS + PUSH_JS, aside=led_html(request.app["state"])
    )


def push_card(request: web.Request) -> str:
    if request.app["push"] is None:
        return ""
    return (
        f'<div class="card" id="push" data-csrf="{esc(request.app["csrf"])}"><h2>Meldingen</h2>'
        '<p class="muted small">Een melding bij elk wedstrijdbericht, ook als de app dicht is. Tik erop om het '
        "bericht direct te delen. Zit je al op de startpagina, dan krijg je geen melding. Per apparaat aanzetten; "
        "werkt via HTTPS, op een iPhone alleen als de app op je beginscherm staat.</p>"
        '<div class="row"><button type="button" id="push-toggle">Meldingen aan</button>'
        '<button type="button" id="push-test" class="secondary" hidden>Testmelding</button></div>'
        '<p id="push-msg" class="small" role="status"></p></div>'
    )


async def share_page(request: web.Request) -> web.Response:
    """Deelscherm voor één bericht (vanuit een pushmelding): direct op Delen tikken."""
    message = request.app["state"].find_message(request.match_info["key"])
    if message is None:  # bv. na een herstart van de app
        raise web.HTTPSeeOther("/")
    body = (
        f'<div class="card"><div id="one-message">{messages_html([message_item(message)])}</div></div>'
        '<p><a href="/">Alle berichten</a></p>'
    )
    return page(request, "Delen", "/", body, script=LIVE_JS, aside=led_html(request.app["state"]))


def settings_form(
    request: web.Request,
    url_error: str = "",
    token_error: str = "",
    url_value: str | None = None,
    wait_error: str = "",
    wait_value: str | None = None,
) -> str:
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
        '<div class="card"><h2>Wachten op de doelpuntmaker</h2>'
        '<p class="muted small">Na een eigen doelpunt wacht de app zo lang op de naam van de doelpuntmaker. '
        "Komt die niet, dan volgt het bericht zonder naam. 0 = niet wachten.</p>"
        '<form method="post" action="/settings">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="goal_wait">'
        '<label for="goal_wait">Wachttijd (seconden)</label>'
        f'<input type="number" id="goal_wait" name="goal_wait" class="num" min="0" max="{MAX_GOAL_WAIT_SECONDS}" '
        'step="1" inputmode="numeric" required '
        f'value="{esc(wait_value if wait_value is not None else int(settings.goal_wait_seconds))}">'
        f'{message_box(wait_error, "error")}'
        '<div class="row"><button type="submit">Wachttijd opslaan</button></div></form></div>'
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
        elif action == "goal_wait":
            settings.set_goal_wait(form.get("goal_wait", ""))
            logger.info("Wachttijd op de doelpuntmaker gewijzigd naar %s seconden", settings.goal_wait_seconds)
        else:
            raise ValueError("Onbekende actie.")
    except ValueError as error:
        if action == "token":
            kwargs = {"token_error": str(error)}
        elif action == "goal_wait":
            kwargs = {"wait_error": str(error), "wait_value": str(form.get("goal_wait", ""))}
        else:
            kwargs = {"url_error": str(error), "url_value": str(form.get("url", ""))}
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
            context["minuut"] = "90+2"
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
    return page(request, "Sjablonen", "/templates", templates_form(request, settings.templates, {}))


async def templates_post(request: web.Request) -> web.Response:
    form = request["form"]
    settings: Settings = request.app["settings"]
    submitted = {
        key: str(form.get(f"tpl_{key}", settings.templates[key])).strip() for key in TEMPLATE_INFO
    }
    errors = {key: error for key in TEMPLATE_INFO if (error := validate_template(key, submitted[key]))}
    if errors:
        response = page(request, "Sjablonen", "/templates", templates_form(request, submitted, errors))
        response.set_status(400)
        return response
    settings.set_templates(submitted)
    logger.info("Berichtsjablonen gewijzigd via de webomgeving")
    raise web.HTTPSeeOther("/templates?msg=templates")


async def templates_reset(request: web.Request) -> web.Response:
    request.app["settings"].reset_templates()
    logger.info("Berichtsjablonen hersteld naar de standaard via de webomgeving")
    raise web.HTTPSeeOther("/templates?msg=reset")


def post_url(text: str) -> str:
    """Link die X opent met de tekst al ingevuld; plaatsen doe je zelf (geen inlog hier)."""
    return POST_URL + quote(text, safe="")


SHARE_ICON = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v13"/></svg>'
)


def messages_html(messages: list[dict]) -> str:
    """Eén blok per bericht: (foto), tijd + teller, tekst, en een rij met Delen en het foto-vinkje."""
    if not messages:
        return '<p class="muted">(nog geen wedstrijdberichten)</p>'
    items = []
    for item in messages:
        too_long = item["length"] > TWEET_LIMIT
        counter = (
            f'<span class="{"too-long" if too_long else ""}">'
            f'{item["length"]}/{TWEET_LIMIT}{" - te lang voor X" if too_long else ""}</span>'
        )
        photo = item.get("photo")
        thumb = toggle = photo_attr = ""
        if photo:
            thumb = f'<img class="thumb" src="{esc(photo)}" alt="Foto van de doelpuntmaker" width="72" height="72">'
            toggle = (
                '<label class="photo-toggle"><input type="checkbox" class="with-photo" checked> '
                "Foto meesturen</label>"
            )
            photo_attr = f' data-photo="{esc(photo)}"'
        items.append(
            f'<li class="msg-item{" has-photo" if photo else ""}" data-key="{esc(item["key"])}" '
            f'data-text="{esc(item["text"])}" data-fallback="{esc(item["url"])}"{photo_attr}>'
            f"{thumb}"
            f'<div class="msg-body"><div class="msg-meta"><span>{esc(item["time"])}</span>{counter}</div>'
            f'<p class="msg-text">{esc(item["text"])}</p></div>'
            f'<div class="msg-actions"><button type="button" class="share">{SHARE_ICON}Delen</button>{toggle}</div>'
            '<p class="item-hint" role="status"></p></li>'
        )
    return f'<ul class="msgs">{"".join(items)}</ul>'


def message_item(message: MatchMessage) -> dict:
    return {
        "time": message.moment.astimezone().strftime("%H:%M:%S"),
        "text": message.text,
        "url": post_url(message.text),
        "length": len(message.text),
        "photo": f"/photo/{photo_id(message.image)}" if message.image else None,
        # Vaste sleutel per bericht: foto-vinkje blijft staan, en de link in een pushmelding.
        "key": message.key,
    }


def live_logs(app: web.Application) -> dict:
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
    messages = [message_item(m) for m in reversed(app["state"].messages)]
    return {
        "messages": messages,
        "messages_html": messages_html(messages),
        "status_html": status_table_html(app),
        "led": {"level": app["state"].level, "status": app["state"].status},
        "app": "\n".join(reversed(app_lines)) or "(nog geen logregels)",
        "note": note,
    }


async def logs_data(request: web.Request) -> web.Response:
    push = request.app["push"]
    endpoint = request.headers.get("X-Push-Endpoint")
    if push is not None and endpoint:
        push.store.mark_present(endpoint)  # startpagina open: geen pushmeldingen naar dit apparaat
    return web.json_response(live_logs(request.app))


async def json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Ongeldige gegevens.") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Ongeldige gegevens.")
    return data


def require_push(request: web.Request):
    push = request.app["push"]
    if push is None:
        raise web.HTTPNotFound(text="Meldingen zijn niet ingesteld.")
    return push


async def push_key(request: web.Request) -> web.Response:
    return web.json_response({"publicKey": require_push(request).public_key})


async def push_subscribe(request: web.Request) -> web.Response:
    push = require_push(request)
    try:
        push.store.add(await json_body(request))
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error)) from None
    logger.info("Apparaat aangemeld voor meldingen (%s in totaal)", len(push.store.subscriptions))
    return web.json_response({"ok": True})


async def push_unsubscribe(request: web.Request) -> web.Response:
    push = require_push(request)
    endpoint = (await json_body(request)).get("endpoint")
    if isinstance(endpoint, str) and push.store.remove(endpoint):
        logger.info("Apparaat afgemeld voor meldingen")
    return web.json_response({"ok": True})


async def push_test(request: web.Request) -> web.Response:
    push = require_push(request)
    subscription = push.store.find(str((await json_body(request)).get("endpoint", "")))
    if subscription is None:
        raise web.HTTPNotFound(text="Dit apparaat is niet aangemeld.")
    result = await push.send_one(subscription, "Testmelding: meldingen werken.", None)
    if result == "gone":
        raise web.HTTPGone(text="Dit apparaat is afgemeld.")
    if result == "failed":
        raise web.HTTPBadGateway(text="Testmelding niet afgeleverd; zie de logging.")
    return web.json_response({"ok": True})


def photo_id(url: str) -> str:
    # "v2": nieuwe adressen, zodat browsers geen eerder gecachte halve foto meer tonen.
    return hashlib.sha256(f"v2:{url}".encode()).hexdigest()[:24]


async def fetch_photo(url: str) -> tuple[bytes, str]:
    """Haal een foto op bij het scorebord (de browser mag dat zelf niet, vanwege CORS)."""
    timeout = ClientTimeout(total=15)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
            if response.status != 200 or not content_type.startswith("image/"):
                raise ValueError(f"geen foto (HTTP {response.status}, {content_type or 'onbekend type'})")
            # Lees tot het einde: content.read(n) geeft alleen wat er al binnen is,
            # waardoor je een halve foto krijgt.
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_PHOTO_BYTES:
                    raise ValueError("foto is te groot")
                chunks.append(chunk)
            data = b"".join(chunks)
            expected = response.content_length
            if expected is not None and len(data) != expected:
                raise ValueError(f"foto onvolledig ontvangen ({len(data)} van {expected} bytes)")
            return data, content_type


async def photo(request: web.Request) -> web.Response:
    """Geef een foto door, maar alleen een die in een van de wedstrijdberichten voorkomt.

    Zo kan dit adres niet worden misbruikt om willekeurige URL's op te halen.
    """
    wanted = request.match_info["photo_id"]
    known = {photo_id(m.image): m.image for m in request.app["state"].messages if m.image}
    url = known.get(wanted)
    if url is None:
        raise web.HTTPNotFound(text="Onbekende foto")
    cache: OrderedDict = request.app["photo_cache"]
    if wanted not in cache:
        try:
            cache[wanted] = await request.app["fetch_photo"](url)
        except Exception as error:  # netwerkfout, geen afbeelding, te groot
            logger.warning("Foto van de doelpuntmaker niet op te halen: %s", error)
            raise web.HTTPBadGateway(text="Foto niet op te halen")
        while len(cache) > PHOTO_CACHE_SIZE:
            cache.popitem(last=False)
    cache.move_to_end(wanted)
    data, content_type = cache[wanted]
    return web.Response(
        body=data,
        content_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


def raw_summary(data: dict) -> str:
    """Korte samenvatting van een ruw bericht: stand en fase van de wedstrijd."""
    sd = data.get("scoreboard_data") if isinstance(data, dict) else None
    if not isinstance(sd, dict):
        return "geen wedstrijdstatus"
    names = {
        team.get("id"): team.get("scoreboard_name") or team.get("name")
        for team in data.get("teams") or []
        if isinstance(team, dict)
    }
    ids = sd.get("teams") if isinstance(sd.get("teams"), dict) else {}
    board = sd.get("scoreboard") if isinstance(sd.get("scoreboard"), dict) else {}
    home = names.get(ids.get("home")) or "Thuis"
    away = names.get(ids.get("away")) or "Uit"
    score = f"{home} {board.get('home', '?')}-{board.get('away', '?')} {away}"
    seconds = sd.get("time")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        clock = f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
    else:
        clock = "?"
    if sd.get("endedAt"):
        phase = "afgelopen"
    elif sd.get("rest"):
        phase = "rust"
    elif sd.get("startedAt"):
        period = sd.get("period") if isinstance(sd.get("period"), int) else 0
        phase = f"{period + 1}e helft {clock}"
    else:
        phase = "niet gestart"
    return f"{score} · {phase}"


def raw_entries_html(lines: list[str]) -> str:
    """De laatste ruwe berichten, per stuk uitklapbaar met ingesprongen JSON van de wedstrijdstatus."""
    if not lines:
        return (
            '<p class="muted">(geen ruwe berichten; staat het logbestand niet op schijf, '
            "dan is de logmap niet schrijfbaar)</p>"
        )
    items = []
    for line in reversed(lines):
        stamp, _, rest = line.partition(" {")
        moment = stamp[11:19] if len(stamp) >= 19 else stamp
        try:
            data = json.loads("{" + rest)
        except ValueError:
            shown = line if len(line) <= RAW_ENTRY_CHARS else f"{line[:RAW_ENTRY_CHARS]} ... [ingekort]"
            items.append(
                f'<details class="raw"><summary><span class="muted">{esc(moment)}</span> '
                f"geen geldige JSON</summary><pre>{esc(shown)}</pre></details>"
            )
            continue
        status = data.get("scoreboard_data") if isinstance(data, dict) else data
        pretty = json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True)
        other = sorted(k for k in data if k != "scoreboard_data") if isinstance(data, dict) else []
        extra = (
            f'<p class="muted small">Weggelaten (staat in het logbestand): {esc(", ".join(other))}.</p>'
            if other
            else ""
        )
        size = f'<span class="muted small">({len(line) // 1024} KB)</span>'
        items.append(
            f'<details class="raw"><summary><span class="muted">{esc(moment)}</span> '
            f"{esc(raw_summary(data))} {size}</summary>"
            f"<pre>{esc(pretty)}</pre>{extra}</details>"
        )
    return "".join(items)


async def logs_page(request: web.Request) -> web.Response:
    live = live_logs(request.app)
    raw_lines = tail_lines(request.app["raw_log"], RAW_LOG_ENTRIES, max_bytes=3_000_000)
    body = (
        '<div class="card"><h2>Berichten en fouten</h2>'
        f'<p id="live-note" class="muted small">{esc(live["note"])}</p>'
        f'<pre id="live-log">{esc(live["app"])}</pre></div>'
        '<div class="card"><h2>Ruwe websocketberichten</h2>'
        f'<p class="muted small">De laatste {RAW_LOG_ENTRIES}, bij het laden van de pagina. Tik op een bericht '
        "voor de wedstrijdstatus (<code>scoreboard_data</code>).</p>"
        f"{raw_entries_html(raw_lines)}</div>"
    )
    return page(request, "Logging", "/logs", body, script=LIVE_JS, aside=led_html(request.app["state"]))


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "connection": request.app["state"].status})


# --- installeerbare webapp ---------------------------------------------------

SERVICE_WORKER_JS = """// Service worker: maakt de app installeerbaar. Er wordt niets opgeslagen
// (de pagina's bevatten persoonlijke gegevens); zonder netwerk volgt een melding.
const OFFLINE = '<!doctype html><html lang="nl"><meta charset="utf-8">'
  + '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Geen verbinding</title>'
  + '<body style="font:16px system-ui,sans-serif;padding:24px">'
  + '<h1>Geen verbinding</h1><p>De scorebord-app is niet bereikbaar. Controleer je netwerk en probeer het opnieuw.</p>';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() =>
    new Response(OFFLINE, {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
// Pushmelding: tonen; bij tikken het deelscherm van dat bericht openen.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {body: event.data ? event.data.text() : ''}; }
  event.waitUntil(self.registration.showNotification(data.title || 'VSCO Scorebord', {
    body: data.body || '', tag: data.tag || undefined, icon: '/static/icon-192.png',
    badge: '/static/icon-192.png', data: {url: data.url || '/'},
  }));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then((list) => {
    for (const client of list) {
      if ('navigate' in client) return client.focus().then(() => client.navigate(url));
    }
    return self.clients.openWindow(url);
  }));
});
"""


async def manifest(request: web.Request) -> web.Response:
    data = {
        "name": "VSCO Scorebord",
        "short_name": "Scorebord",
        "description": "Wedstrijdberichten van het VSCO-scorebord, klaar om te delen.",
        "lang": "nl",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#14171b",
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return web.json_response(
        data, content_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"}
    )


async def service_worker(request: web.Request) -> web.Response:
    return web.Response(
        text=SERVICE_WORKER_JS,
        content_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# --- inloggen ----------------------------------------------------------------


def load_session_secret(path: Path) -> bytes:
    """Geheime sleutel voor sessiecookies; bewaard, zodat je na een herstart ingelogd blijft."""
    try:
        secret = bytes.fromhex(path.read_text(encoding="utf-8").strip())
        if len(secret) >= 32:
            return secret
    except (OSError, ValueError):
        pass
    secret = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret.hex(), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as error:
        logger.warning("Sessiesleutel niet op te slaan (%s); na een herstart opnieuw inloggen", error)
    return secret


def _session_signature(app: web.Application, payload: str) -> str:
    # De sleutel hangt ook af van gebruikersnaam en wachtwoord: wijzig je die,
    # dan zijn alle bestaande sessies direct ongeldig.
    key = hmac.new(
        app["session_secret"], f"{app['username']}\0{app['password']}".encode(), hashlib.sha256
    ).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session(app: web.Application, now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + app["session_days"] * 86400)
    payload = str(expires)
    return f"{payload}.{_session_signature(app, payload)}"


def valid_session(app: web.Application, value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    payload, signature = value.split(".", 1)
    if not hmac.compare_digest(signature, _session_signature(app, payload)):
        return False
    try:
        return int(payload) > (time.time() if now is None else now)
    except ValueError:
        return False


def is_authorized(request: web.Request, username: str, password: str) -> bool:
    """HTTP Basic, voor scripts en tools (de browser gebruikt de inlogpagina)."""
    header = request.headers.get("Authorization", "")
    try:
        auth = BasicAuth.decode(header, encoding="utf-8")
    except ValueError:
        return False
    user_ok = hmac.compare_digest(auth.login.encode(), username.encode())
    pass_ok = hmac.compare_digest(auth.password.encode(), password.encode())
    return user_ok and pass_ok


def is_https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def safe_next(value: str | None) -> str:
    """Alleen terugsturen naar een pagina van deze app (geen open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def login_form(request: web.Request, error: str = "", next_url: str = "/") -> str:
    return (
        '<div class="card login">'
        f"{message_box(error, 'error')}"
        '<form method="post" action="/login">'
        f'{csrf_field(request)}<input type="hidden" name="next" value="{esc(next_url)}">'
        '<label for="username">Gebruikersnaam</label>'
        '<input type="text" id="username" name="username" required autocomplete="username" autocapitalize="none">'
        '<label for="password">Wachtwoord</label>'
        '<input type="password" id="password" name="password" required autocomplete="current-password">'
        f'<p class="muted small">Je blijft {request.app["session_days"]} dagen ingelogd op dit apparaat.</p>'
        '<div class="row"><button type="submit">Inloggen</button></div></form></div>'
    )


async def login_page(request: web.Request) -> web.Response:
    if valid_session(request.app, request.cookies.get(SESSION_COOKIE)):
        raise web.HTTPSeeOther(safe_next(request.query.get("next")))
    form = login_form(request, next_url=safe_next(request.query.get("next")))
    return page(request, "Inloggen", "", form, show_nav=False)


async def login_post(request: web.Request) -> web.Response:
    form = request["form"]
    app = request.app
    next_url = safe_next(str(form.get("next", "/")))
    user_ok = hmac.compare_digest(str(form.get("username", "")).encode(), app["username"].encode())
    pass_ok = hmac.compare_digest(str(form.get("password", "")).encode(), app["password"].encode())
    if not (user_ok and pass_ok):
        await asyncio.sleep(1)  # remt het raden van wachtwoorden
        logger.warning("Mislukte inlogpoging in de webomgeving")
        response = page(
            request, "Inloggen", "", login_form(request, "Onjuiste gebruikersnaam of wachtwoord.", next_url),
            show_nav=False,
        )
        response.set_status(401)
        return response
    response = web.HTTPSeeOther(next_url)
    response.set_cookie(
        SESSION_COOKIE,
        make_session(app),
        max_age=app["session_days"] * 86400,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=is_https(request),
    )
    raise response


async def logout_post(request: web.Request) -> web.Response:
    response = web.HTTPSeeOther("/login")
    response.del_cookie(SESSION_COOKIE, path="/")
    raise response


# --- beveiliging ------------------------------------------------------------


def wants_html(request: web.Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("Accept", "")


@web.middleware
async def security(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    app = request.app
    request["nonce"] = secrets.token_urlsafe(16)
    public = request.path in PUBLIC_PATHS or request.path.startswith("/static/")
    if not public and not valid_session(app, request.cookies.get(SESSION_COOKIE)):
        if "Authorization" in request.headers:
            if not is_authorized(request, app["username"], app["password"]):
                await asyncio.sleep(1)  # remt het raden van wachtwoorden
                return web.Response(status=401, text="Onjuiste inloggegevens")
        elif wants_html(request):
            raise web.HTTPSeeOther(f"/login?{urlencode({'next': request.path_qs})}")
        else:
            return web.Response(status=401, text="Inloggen vereist")
    if request.method == "POST":
        form = await request.post()
        token = str(form.get("csrf", "") or request.headers.get("X-CSRF-Token", ""))
        if not hmac.compare_digest(token.encode(), app["csrf"].encode()):
            return web.Response(status=403, text="Ongeldig formulier; laad de pagina opnieuw.")
        request["form"] = form
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error
    response.headers.setdefault("Cache-Control", "no-store")  # foto's en app-bestanden zetten hun eigen
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{request['nonce']}'; "
        "connect-src 'self'; img-src 'self'; manifest-src 'self'; worker-src 'self'; "
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
    photo_fetcher=None,
    session_secret: bytes | None = None,
    session_days: int = DEFAULT_SESSION_DAYS,
    push=None,
) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app["push"] = push  # PushSender, of None als meldingen uit staan
    app["fetch_photo"] = photo_fetcher or fetch_photo  # vervangbaar in tests
    app["photo_cache"] = OrderedDict()
    app["settings"] = settings
    app["state"] = state
    app["app_log"] = app_log
    app["raw_log"] = raw_log
    app["memory_log"] = memory_log
    app["username"] = username
    app["password"] = password
    app["csrf"] = secrets.token_urlsafe(32)
    app["session_secret"] = session_secret or secrets.token_bytes(32)
    app["session_days"] = session_days
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/login", login_page),
            web.post("/login", login_post),
            web.post("/logout", logout_post),
            web.get("/manifest.webmanifest", manifest),
            web.get("/sw.js", service_worker),
            web.static("/static", STATIC_DIR, append_version=False),
            web.get("/", messages_page),
            web.get("/status", status_page),
            web.get("/settings", settings_page),
            web.post("/settings", settings_post),
            web.get("/templates", templates_page),
            web.post("/templates", templates_post),
            web.post("/templates/reset", templates_reset),
            web.get("/logs", logs_page),
            web.get("/logs/data", logs_data),
            web.get("/photo/{photo_id:[0-9a-f]{24}}", photo),
            web.get("/share/{key:[0-9a-f]{12}}", share_page),
            web.get("/push/key", push_key),
            web.post("/push/subscribe", push_subscribe),
            web.post("/push/unsubscribe", push_unsubscribe),
            web.post("/push/test", push_test),
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
    session_secret: bytes | None = None,
    session_days: int = DEFAULT_SESSION_DAYS,
    push=None,
) -> web.AppRunner:
    app = create_web_app(
        settings, state, app_log, raw_log, username, password, memory_log,
        session_secret=session_secret, session_days=session_days, push=push,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Webomgeving bereikbaar op http://%s:%s", host, port)
    return runner
