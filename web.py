"""Webomgeving van MatchShareNL: berichten delen, status, bronnen, sjablonen en logging.

Beveiliging: alles behalve de inlogpagina, /health en de app-bestanden (manifest,
service worker, iconen) vereist inloggen. Dat gaat via de inlogpagina met een
ondertekend sessiecookie (HttpOnly, SameSite=Lax, standaard 30 dagen), of voor
scripts via HTTP Basic. Formulieren hebben een CSRF-token, scripts zijn alleen
met een nonce per pagina toegestaan en alle uitvoer wordt ge-escaped. Het token
staat alleen onder Connectie > LED Scores (verborgen tot je het oog-icoon gebruikt).
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
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlencode

from aiohttp import BasicAuth, ClientSession, ClientTimeout, web

from messages import (
    MAX_HASHTAG_LENGTH,
    MAX_TEMPLATE_LENGTH,
    PLACEHOLDERS,
    SAMPLE_CONTEXT,
    TEMPLATE_INFO,
    default_hashtag,
    render_message,
    validate_template,
)
from settings import (
    MAX_GOAL_WAIT_SECONDS,
    MAX_POLL_SECONDS,
    MIN_POLL_SECONDS,
    Settings,
)
from sources.scoreboard import connection_rows, display_url
from sources.voetbalnederland import VoetbalnederlandError
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
    "theme": "Weergave opgeslagen.",
    "url": "URL opgeslagen. De verbinding wordt opnieuw opgebouwd.",
    "token": "Token opgeslagen. De verbinding wordt opnieuw opgebouwd.",
    "templates": "Sjablonen opgeslagen.",
    "reset": "Sjablonen hersteld naar de standaardteksten.",
    "goal_wait": "Wachttijd opgeslagen. Hij geldt vanaf het volgende doelpunt.",
    "source": "Bron en club opgeslagen.",
    "team": "Club opgeslagen. Het programma wordt opnieuw opgehaald.",
    "club_name": "Clubnaam opgeslagen.",
    "poll": "Interval opgeslagen.",
    "voetbalnederland_url": "Adres van de Voetbalnederland-bron opgeslagen.",
    "hashtag": "Hashtag opgeslagen. Hij geldt voor de wedstrijd die nu bezig is, anders voor de eerstvolgende.",
    "hashtag_reset": "Hashtag hersteld naar de standaard.",
}

# Kleuren voor donker: bij "Systeem" als het apparaat donker staat, en altijd bij "Donker".
DARK_VARS = (
    "--bg:#14171b;--card:#1d2127;--text:#e6e9ed;--muted:#9aa5b1;--line:#2f353d;--accent:#6ea8ff;"
    "--on-accent:#0b1526;--ok:#4cc26a;--warn:#e0b341;--err:#ff7b72;--code:#262b32;color-scheme:dark;"
    "--watermark:url(/static/watermark-dark.svg)"
)
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5b6670;--line:#d9dee4;
--accent:#1f5fbf;--on-accent:#fff;--ok:#1a7f37;--warn:#9a6700;--err:#c4302b;--code:#eef1f4;color-scheme:light;
--watermark:url(/static/watermark-light.svg)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){@@DARK@@}}
:root[data-theme=dark]{@@DARK@@}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;background:var(--watermark) center 45%/min(92vw,560px) no-repeat}
.logout{margin-top:4px}
.app-footer{position:fixed;left:0;right:0;bottom:0;z-index:20;background:var(--card);border-top:1px solid var(--line);
padding-bottom:env(safe-area-inset-bottom)}
.app-footer-inner{max-width:60rem;margin:0 auto;height:60px;padding:0 8px 0 12px;display:grid;
grid-template-columns:1fr auto 1fr;align-items:center}
.brand-logo{display:inline-flex;width:44px;height:44px;align-items:center}
.brand-logo img{width:36px;height:36px;display:block}
.brand-name{font-weight:700;letter-spacing:.02em}
.app-menu{justify-self:end;position:relative}
.app-menu summary{list-style:none;display:flex;align-items:center;justify-content:center;width:48px;height:48px;
border-radius:50%;color:var(--text);cursor:pointer}
.app-menu[open] summary,.app-menu summary:hover{background:var(--bg)}
.app-menu summary::-webkit-details-marker{display:none}
.menu-list{position:absolute;right:0;bottom:56px;min-width:12rem;background:var(--card);border:1px solid var(--line);
border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.25);padding:4px 0;display:flex;flex-direction:column}
.menu-list a{padding:12px 16px;min-height:44px;color:var(--text);text-decoration:none}
.menu-list a.active{font-weight:600;color:var(--accent)}
.menu-list hr{border:0;border-top:1px solid var(--line);margin:4px 0}
.login{max-width:24rem}
.titlebar{display:flex;align-items:center;justify-content:space-between;gap:8px 12px;flex-wrap:wrap;margin:8px 0 12px}
.titlebar h1{margin:0}
.brand-plate{background:#14171b;border-radius:10px;padding:14px 16px;margin:0 0 12px;display:flex;justify-content:center}.brand-plate img{max-width:100%;height:auto;display:block}
.titlebar-end{display:flex;align-items:center;gap:8px;margin-left:auto}
body.base main,body.sheet main{padding-bottom:84px}
body.sheet .titlebar{position:sticky;top:0;z-index:10;background:var(--bg);margin:0 -16px 12px;padding:8px 16px;
border-bottom:1px solid var(--line)}
a.close{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;
color:var(--text);margin-right:-8px}
a.close:hover{background:var(--card)}
body.sheet main{animation:sheet-in .2s ease-out}
@keyframes sheet-in{from{transform:translateY(24px);opacity:0}to{transform:none;opacity:1}}
@media (prefers-reduced-motion:reduce){body.sheet main{animation:none}}
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
details.block{background:var(--card);border:1px solid var(--line);border-radius:8px;margin-bottom:12px}
details.block>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:10px;min-height:52px;
padding:0 16px;font-weight:600;font-size:1.05rem}
details.block>summary::-webkit-details-marker{display:none}
details.block>summary::before{content:"";width:7px;height:7px;border-right:2px solid currentColor;
border-bottom:2px solid currentColor;transform:rotate(-45deg);transition:transform .15s}
details.block[open]>summary::before{transform:rotate(45deg)}
.block-body{padding:0 16px 16px}.block-body h3{font-size:1rem;margin:16px 0 4px}
.block-body h3:first-child{margin-top:0}.block-body form+h3{margin-top:24px}
label.choice{display:flex;align-items:center;gap:10px;min-height:44px;font-weight:400;margin:0}
label.choice input{width:20px;height:20px;margin:0;accent-color:var(--accent)}
label.choice-source{align-items:flex-start;padding:8px 0}label.choice-source input{margin-top:3px;flex:none}
form.result{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0;
border-bottom:1px solid var(--line)}form.result button{padding:6px 14px}
.club{font-size:1.05rem;margin:0 0 12px}
.note{border-left:3px solid var(--warn);padding:6px 10px;margin:0 0 12px;color:var(--muted);font-size:.92rem}
details.raw{border-bottom:1px solid var(--line);padding:8px 0}details.raw:last-child{border-bottom:0}
details.raw summary{cursor:pointer;word-break:break-word}details.raw pre{margin-top:8px;max-height:60vh}input.num{max-width:8rem}
main{max-width:60rem;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:8px 0 16px}h2{font-size:1.1rem;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{width:14rem;color:var(--muted);font-weight:500}
label{display:block;font-weight:600;margin:12px 0 4px}
select,input[type=search],input[type=text],input[type=password],input[type=url],input[type=number]{width:100%;padding:8px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--text);font:inherit}
select{min-height:44px}
[hidden]{display:none!important}
.team-results{margin-top:4px}
label.team-pick{align-items:flex-start;padding:6px 0;line-height:1.3}label.team-pick input{margin-top:2px;flex:none}
label.team-pick .muted{font-size:.85rem}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
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
CSS = CSS.replace("@@DARK@@", DARK_VARS)

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

# Weergave per apparaat, in een cookie (zodat de server direct het goede thema stuurt).
THEME_COOKIE = "theme"
THEMES = {"system": "Systeem", "light": "Licht", "dark": "Donker"}


def theme_of(request: web.Request) -> str:
    chosen = request.cookies.get(THEME_COOKIE, "")
    return chosen if chosen in THEMES else "system"


ROLE_ADMIN = "admin"  # beheerder: alles, ook de app-instellingen
ROLE_USER = "user"  # gebruiker: berichten delen en eigen instellingen
ROLE_LABELS = {ROLE_ADMIN: "beheerder", ROLE_USER: "gebruiker"}
# Alleen voor de beheerder (ook de oude adressen, die naar /app doorsturen).
ADMIN_PATHS = {"/app", "/sjablonen", "/teams", "/status", "/settings", "/templates", "/templates/reset", "/logs"}


def nav_items(role: str | None) -> tuple[tuple[str, str], ...]:
    items = (("/", "Berichten"),)
    return items + (("/app", "Connectie"), ("/sjablonen", "Sjablonen")) if role == ROLE_ADMIN else items


# Berichten is de basis; alle andere pagina's openen als paneel erover, met een kruisje terug.
BASE_PATH = "/"
CLOSE_ICON = (
    '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>'
)
# Escape sluit een paneel (behalve tijdens het typen in een veld).
SHEET_JS = (
    "document.addEventListener('keydown',function(e){if(e.key!=='Escape')return;"
    "var t=e.target&&e.target.tagName;if(t==='INPUT'||t==='TEXTAREA'||t==='SELECT')return;location.href='/';});"
)
# Tweede deel van het menu: per gebruiker en apparaat.
ACCOUNT_ITEMS = (("/pushmeldingen", "Pushmeldingen"), ("/account", "Account"))
MENU_ICON = (
    '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>'
)


def footer_html(role: str | None, active: str) -> str:
    """Vaste footer op elke pagina: logo (naar Berichten), de naam van de app en het menu."""
    return (
        '<footer class="app-footer"><div class="app-footer-inner">'
        f'<a href="{BASE_PATH}" class="brand-logo" aria-label="Naar de berichten">'
        '<img src="/static/logo.svg" alt="" width="36" height="36"></a>'
        '<span class="brand-name">MatchShareNL</span>'
        f"{menu_html(role, active)}</div></footer>"
    )


def menu_html(role: str | None, active: str) -> str:
    """Het menu (hamburger, rechts in de footer) met de panelen die over Berichten
    openen: eerst die van de app, dan die van het account."""
    def links(items: tuple[tuple[str, str], ...]) -> str:
        return "".join(
            f'<a href="{path}"{" class=active aria-current=page" if path == active else ""}>{label}</a>'
            for path, label in items
        )

    app_links = links(tuple(item for item in nav_items(role) if item[0] != BASE_PATH))
    app_links += "<hr>" if app_links else ""
    return (
        '<details class="app-menu" id="menu">'
        f'<summary aria-label="Menu" title="Menu">{MENU_ICON}</summary>'
        f'<nav class="menu-list" aria-label="Menu">{app_links}{links(ACCOUNT_ITEMS)}</nav></details>'
    )


# Menu sluiten bij een tik ernaast of op Escape.
MENU_JS = (
    "(function(){var m=document.getElementById('menu');if(!m)return;"
    "document.addEventListener('click',function(e){if(m.open&&!m.contains(e.target))m.open=false;});"
    "document.addEventListener('keydown',function(e){if(e.key==='Escape')m.open=false;});})();"
)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


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
    '<link rel="icon" href="/static/logo.svg" type="image/svg+xml">'
    '<link rel="icon" href="/static/icon-192.png" type="image/png">'
    '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-title" content="MatchShareNL">'
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
    base = show_nav and active == BASE_PATH
    sheet = show_nav and not base
    nav = footer_html(request.get("role"), active) if show_nav else ""
    close = (
        f'<a href="{BASE_PATH}" class="close" aria-label="Sluiten" title="Sluiten">{CLOSE_ICON}</a>' if sheet else ""
    )
    extra_js = (MENU_JS if show_nav else "") + (SHEET_JS if sheet else "")
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    nonce = esc(request["nonce"])
    script_tag = f'<script nonce="{nonce}">{SW_REGISTER_JS}{extra_js}{script}</script>'
    body_class = ' class="base"' if base else ' class="sheet"' if sheet else ""
    theme = theme_of(request)
    theme_attr = f' data-theme="{theme}"' if theme != "system" else ""
    document = (
        f'<!doctype html><html lang="nl"{theme_attr}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f"{APP_HEAD}{meta}<title>{esc(title)} - MatchShareNL</title><style>{CSS}</style></head>"
        f'<body{body_class}><main><div class="titlebar"><h1>{esc(title)}</h1>'
        f'<div class="titlebar-end">{aside}{close}</div></div>{body}</main>'
        f"{nav}{script_tag}</body></html>"
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
    """Statustabel van de actieve bron (zonder bronnen: die van het scorebord)."""
    manager = app.get("sources")
    if manager is not None:
        source = manager.active()
        rows = [("Bron", source.label, None), ("Je club", source.club() or source.club_hint, None)]
        rows += source.status_rows()
    else:
        rows = connection_rows(app["settings"], app["state"])
    cells = []
    for label, text, level in rows:
        value = f'<span class="pill {esc(level)}">{esc(text)}</span>' if level else esc(text)
        cells.append(f"<tr><th>{esc(label)}</th><td>{value}</td></tr>")
    return "<table>" + "".join(cells) + "</table>"


def led_state(app: web.Application) -> tuple[str, str]:
    """(niveau, tekst) voor het lampje: de status van de actieve bron."""
    manager = app.get("sources")
    if manager is not None:
        return manager.active().led()
    state: ConnectionState = app["state"]
    return state.level, state.status


def led_html(request: web.Request) -> str:
    """Statuslampje, live bijgewerkt. Voor de beheerder een link naar de status onder Connectie."""
    level, status = led_state(request.app)
    inner = (
        f'<span class="led {esc(level)}" aria-hidden="true"></span>'
        f'<span id="led-text">{esc(status)}</span>'
    )
    if request.get("role") == ROLE_ADMIN:
        return f'<a id="led" class="led-bar" href="/app#status" title="Status van de verbinding">{inner}</a>'
    return f'<span id="led" class="led-bar" title="Status van de verbinding">{inner}</span>'


async def messages_page(request: web.Request) -> web.Response:
    live = live_logs(request.app)
    body = (
        '<details class="intro"><summary>Hoe werkt delen?</summary>'
        "<p>De berichten sinds de laatste start, nieuwste bovenaan. Ze verschijnen vanzelf. "
        '"Delen" opent het deelmenu van je telefoon (via HTTPS), met de foto als "Foto meesturen" '
        "is aangevinkt. Zonder deelmenu, bijvoorbeeld op een pc, opent X met de tekst al ingevuld.</p></details>"
        f'<div class="card"><div id="live-messages">{live["messages_html"]}</div></div>'
    )
    return page(request, "Berichten", "/", body, script=LIVE_JS, aside=led_html(request))


async def push_page(request: web.Request) -> web.Response:
    """Pushmeldingen voor dit apparaat (per gebruiker en apparaat)."""
    body = push_card(request) or '<div class="card"><p class="muted">Pushmeldingen zijn niet ingesteld.</p></div>'
    return page(request, "Pushmeldingen", "/pushmeldingen", body, script=LIVE_JS + PUSH_JS, aside=led_html(request))


async def account_page(request: web.Request) -> web.Response:
    """Account: met welke rol je op dit apparaat bent ingelogd, en uitloggen."""
    role = request.get("role")
    current = theme_of(request)
    radios = "".join(
        f'<label class="choice"><input type="radio" name="theme" value="{value}"'
        f'{" checked" if value == current else ""}>{label}</label>'
        for value, label in THEMES.items()
    )
    body = (
        f"{flash(request)}"
        '<div class="card"><h2>Weergave</h2>'
        '<p class="muted small">Systeem volgt de instelling van je apparaat (licht of donker). '
        "Geldt voor dit apparaat.</p>"
        '<form method="post" action="/theme" id="theme-form">'
        f'{csrf_field(request)}{radios}'
        '<div class="row"><button type="submit">Weergave opslaan</button></div></form></div>'
        '<div class="card"><h2>Account</h2>'
        f'<p>Ingelogd als <strong>{esc(ROLE_LABELS.get(role, role))}</strong> op dit apparaat.</p>'
        '<form method="post" action="/logout" class="logout">'
        f'{csrf_field(request)}<button type="submit" class="secondary">Uitloggen</button></form></div>'
    )
    return page(request, "Account", "/account", body, script=LIVE_JS + THEME_JS, aside=led_html(request))


# Een andere weergave kiezen slaat direct op (de knop blijft voor zonder JavaScript).
THEME_JS = (
    "(function(){var f=document.getElementById('theme-form');if(!f)return;"
    "f.addEventListener('change',function(){f.submit();});})();"
)


async def theme_post(request: web.Request) -> web.Response:
    """Weergave voor dit apparaat opslaan (Systeem, Licht of Donker)."""
    chosen = str(request["form"].get("theme", ""))
    if chosen not in THEMES:
        raise web.HTTPBadRequest(text="Onbekende weergave.")
    response = web.HTTPSeeOther("/account?msg=theme")
    if chosen == "system":
        response.del_cookie(THEME_COOKIE, path="/")
    else:
        response.set_cookie(
            THEME_COOKIE, chosen, max_age=400 * 86400, path="/", httponly=True, samesite="Lax",
            secure=is_https(request),
        )
    raise response


def push_card(request: web.Request) -> str:
    if request.app["push"] is None:
        return ""
    return (
        f'<div class="card" id="push" data-csrf="{esc(request.app["csrf"])}">'
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
    )
    return page(request, "Delen", "/share", body, script=LIVE_JS, aside=led_html(request))


def connection_html(
    request: web.Request, url_error: str = "", token_error: str = "", url_value: str | None = None
) -> str:
    settings: Settings = request.app["settings"]
    shown = display_url(settings.url) if url_value is None else url_value
    return (
        '<h3>URL</h3><form method="post" action="/settings">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="url">'
        '<label for="url">Websocket-URL (ws:// of wss://, zonder token)</label>'
        f'<input type="url" id="url" name="url" value="{esc(shown)}" required maxlength="2048" autocomplete="off">'
        f'{message_box(url_error, "error")}'
        '<div class="row"><button type="submit">URL opslaan</button></div></form>'
        '<h3>Token</h3><form method="post" action="/settings">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="token">'
        '<label for="token">Token</label>'
        '<div class="pw"><input type="password" id="token" name="token" '
        f'value="{esc(settings.token)}" required maxlength="4096" autocomplete="off" spellcheck="false">'
        '<button type="button" class="reveal" data-target="token" title="Toon token" '
        f'aria-label="Toon token" aria-pressed="false">{EYE_ICONS}</button></div>'
        f'{message_box(token_error, "error")}'
        '<div class="row"><button type="submit">Token opslaan</button></div></form>'
    )


def goal_wait_html(request: web.Request, wait_error: str = "", wait_value: str | None = None) -> str:
    settings: Settings = request.app["settings"]
    return (
        '<p class="muted small">Na een eigen doelpunt wacht de app zo lang op de naam van de doelpuntmaker. '
        "Komt die niet, dan volgt het bericht zonder naam. 0 = niet wachten.</p>"
        '<form method="post" action="/settings">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="goal_wait">'
        '<label for="goal_wait">Wachttijd op de doelpuntmaker (seconden)</label>'
        f'<input type="number" id="goal_wait" name="goal_wait" class="num" min="0" max="{MAX_GOAL_WAIT_SECONDS}" '
        'step="1" inputmode="numeric" required '
        f'value="{esc(wait_value if wait_value is not None else int(settings.goal_wait_seconds))}">'
        f'{message_box(wait_error, "error")}'
        '<div class="row"><button type="submit">Wachttijd opslaan</button></div></form>'
    )


def hashtag_html(request: web.Request, hashtag_error: str = "", hashtag_value: str | None = None) -> str:
    """Blok Hashtag: de standaard uitleggen en een eigen hashtag voor één wedstrijd instellen."""
    settings: Settings = request.app["settings"]
    manager = request.app.get("sources")
    match = manager.active().match if manager is not None else None
    if isinstance(match, dict) and match.get("homeClub") and match.get("awayClub"):
        home, away = str(match["homeClub"]), str(match["awayClub"])
        known = (
            f'<p class="small">Wedstrijd: <strong>{esc(home)} - {esc(away)}</strong> '
            f"(standaard <code>{esc(default_hashtag(home, away))}</code>)</p>"
        )
    else:
        known = '<p class="small">Wedstrijd: nog niet bekend bij de bron.</p>'
    current = (
        f"Eigen hashtag ingesteld: <code>{esc(settings.hashtag_override)}</code>. "
        "Hij vervalt na het einde van deze wedstrijd."
        if settings.hashtag_override
        else "Nu is de standaard actief."
    )
    value = hashtag_value if hashtag_value is not None else settings.hashtag_override
    return (
        '<p class="muted small">De hashtag onder elk bericht ({hashtag}) is standaard de eerste drie letters '
        "van de thuisploeg plus de eerste drie letters van de uitploeg, bijvoorbeeld "
        "<code>#vscvsw</code>. Wil je voor één wedstrijd een andere, vul die dan hier in. Hij geldt voor de "
        "wedstrijd van je club die nu bezig is, anders voor de eerstvolgende (hieronder, als de bron hem al "
        "kent), en vervalt na het einde ervan.</p>"
        f"{known}"
        f'<p class="small">{current}</p>'
        '<form method="post" action="/settings">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="hashtag">'
        '<label for="hashtag_input">Eigen hashtag (met of zonder #)</label>'
        f'<input type="text" id="hashtag_input" name="hashtag" maxlength="{MAX_HASHTAG_LENGTH + 1}" '
        'autocapitalize="none" autocorrect="off" spellcheck="false" autocomplete="off" '
        f'placeholder="#vscvsw" value="{esc(value)}">'
        f'{message_box(hashtag_error, "error")}'
        '<div class="row"><button type="submit">Hashtag opslaan</button></div></form>'
        '<form method="post" action="/settings" class="row">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="hashtag_reset">'
        '<button type="submit" class="secondary">Standaard gebruiken</button></form>'
    )


def source_html(
    request: web.Request, error: str = "", team: dict | None = None, chosen: str | None = None
) -> str:
    """Blok Bron en club: bron en club in één formulier, samen opgeslagen.

    Bij het scorebord ligt de club vast; bij Voetbalnederland kies je hem met een
    live-zoekveld (TEAM_JS). Zonder JavaScript zoekt de knop Zoeken via de server.
    """
    manager = request.app.get("sources")
    if manager is None:
        return '<p class="muted">Er zijn geen bronnen beschikbaar.</p>'
    chosen = chosen if chosen in manager.sources else manager.active_id
    radios = "".join(
        f'<label class="choice choice-source"><input type="radio" name="source" value="{esc(source_id)}"'
        f'{" checked" if chosen == source_id else ""}>'
        f'<span><strong>{esc(source.label)}</strong><br><span class="muted small">{esc(source.description)}</span>'
        "</span></label>"
        for source_id, source in manager.sources.items()
    )
    clubs = "".join(
        f'<div class="club" data-source="{esc(source_id)}"{"" if source_id == chosen else " hidden"}>'
        + (team_picker_html(request, **(team or {})) if source_id == "voetbalnederland" else fixed_club_html(source))
        + "</div>"
        for source_id, source in manager.sources.items()
    )
    return (
        '<form method="post" action="/settings" id="source-form">'
        f'{csrf_field(request)}<input type="hidden" name="action" value="source">{radios}'
        f"<h3>Je club</h3>{clubs}"
        f'{message_box(error, "error")}'
        '<div class="row"><button type="submit" name="save" value="1">Opslaan</button></div></form>'
    )


def fixed_club_html(source) -> str:
    club = source.club()
    shown = f"<strong>{esc(club)}</strong>" if club else f'<span class="muted">{esc(source.club_hint)}</span>'
    return f'<p>{shown}</p><p class="muted small">Bij {esc(source.label)} ligt de club vast: de thuisploeg.</p>'


TEAM_RESULTS = 8  # zoveel clubs tonen, om het blok kort te houden


def team_label(club: dict) -> tuple[str, str]:
    detail = " \u00b7 ".join(str(v) for v in (club.get("speeldag"), club.get("competitionName")) if v)
    return str(club.get("name", "")), detail


def team_option(team_id: object, name: str, detail: str = "", checked: bool = False) -> str:
    return (
        f'<label class="choice team-pick"><input type="radio" name="team" value="{esc(team_id)}:{esc(name)}"'
        f'{" checked" if checked else ""}><span><strong>{esc(name)}</strong>'
        f'{f"<br><span class=muted>{esc(detail)}</span>" if detail else ""}</span></label>'
    )


def team_picker_html(
    request: web.Request, error: str = "", results: list[dict] | None = None, query: str = ""
) -> str:
    """Club bij Voetbalnederland: de huidige club (gekozen) en zoekresultaten als keuzes."""
    settings: Settings = request.app["settings"]
    current = (
        team_option(settings.team_id, settings.team_name or str(settings.team_id), checked=True)
        if settings.team_id else '<p class="muted small" id="team-none">Nog geen club gekozen.</p>'
    )
    found = "".join(
        team_option(club.get("id", ""), *team_label(club))
        for club in (results or [])[:TEAM_RESULTS]
        if str(club.get("id")) != str(settings.team_id)
    )
    if results is not None and not results:
        found = '<p class="muted small">Geen clubs gevonden.</p>'
    return (
        f'<div id="team-current">{current}</div>'
        f'<label for="team_q">{"Andere club zoeken" if settings.team_id else "Zoek je club"}</label>'
        f'<input type="search" id="team_q" name="q" value="{esc(query)}" maxlength="60" '
        'autocomplete="off" placeholder="Typ een clubnaam, bv. VSCO" enterkeyhint="search">'
        f'{message_box(error, "error")}'
        f'<div id="team-results" class="team-results" aria-live="polite">{found}</div>'
        '<div class="row" id="team-search-row"><button type="submit" name="search" value="1" '
        'class="secondary" formnovalidate>Zoeken</button></div>'
    )


# Bron wisselen toont direct het clubdeel van die bron (opslaan gebeurt met Opslaan).
# Live zoeken: resultaten als keuzes; Enter in het zoekveld zoekt in plaats van op te slaan.
TEAM_JS = (
    "(function(){var f=document.getElementById('source-form');if(!f)return;"
    "function sync(){var c=f.querySelector('input[name=source]:checked');"
    "f.querySelectorAll('.club').forEach(function(d){d.hidden=!c||d.dataset.source!==c.value;});}"
    "f.querySelectorAll('input[name=source]').forEach(function(r){r.addEventListener('change',sync);});sync();"
    "var q=document.getElementById('team_q');if(!q)return;"
    "var box=document.getElementById('team-results'),row=document.getElementById('team-search-row'),"
    "t=null,ctl=null,n=0;if(row)row.hidden=true;"
    "function note(text){box.textContent='';var p=document.createElement('p');p.className='muted small';"
    "p.textContent=text;box.appendChild(p);}"
    "function show(list){box.textContent='';var cur=f.querySelector('#team-current input');"
    "list=list.filter(function(c){return !cur||cur.value.split(':')[0]!==String(c.id);});"
    "if(!list.length){note('Geen andere clubs gevonden.');return;}"
    "list.forEach(function(c){var l=document.createElement('label');l.className='choice team-pick';"
    "var i=document.createElement('input');i.type='radio';i.name='team';i.value=c.id+':'+c.name;"
    "var s=document.createElement('span'),b=document.createElement('strong');b.textContent=c.name;s.appendChild(b);"
    "if(c.detail){s.appendChild(document.createElement('br'));var d=document.createElement('span');"
    "d.className='muted';d.textContent=c.detail;s.appendChild(d);}"
    "l.appendChild(i);l.appendChild(s);box.appendChild(l);});}"
    "function search(){var v=q.value.trim(),me=++n;if(ctl)ctl.abort();"
    "if(v.length<2){box.textContent='';return;}var c=ctl=new AbortController();"
    "var timer=setTimeout(function(){c.abort();},8000);"
    "fetch('/teams?q='+encodeURIComponent(v),{signal:c.signal,credentials:'same-origin'})"
    ".then(function(r){if(!r.ok)throw new Error();return r.json();})"
    ".then(function(list){if(me===n)show(list);})"
    ".catch(function(){if(me===n)note('Zoeken lukt nu niet.');})"
    ".finally(function(){clearTimeout(timer);});}"
    "q.addEventListener('input',function(){clearTimeout(t);t=setTimeout(search,300);});"
    "q.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();clearTimeout(t);search();}});"
    "})();"
)


def voetbalnederland_html(request: web.Request, error: str = "", error_field: str = "") -> str:
    """Blok Voetbalnederland: clubnaam, interval en het adres van de API (de club staat onder Bron)."""
    settings: Settings = request.app["settings"]

    def form(action: str, inner: str, button: str) -> str:
        err = message_box(error, "error") if error and error_field == action else ""
        return (
            f'<form method="post" action="/settings">{csrf_field(request)}'
            f'<input type="hidden" name="action" value="{action}">{inner}{err}'
            f'<div class="row"><button type="submit">{esc(button)}</button></div></form>'
        )

    return (
        '<p class="muted small">Volgt de wedstrijd van vandaag van je club (onder Bron), thuis of uit '
        f"(elke {esc(settings.poll_seconds)} seconden, vanaf een kwartier voor de aftrap). Begin, rust, "
        "tweede helft en einde hebben geen minuut; doelpunten wel.</p>"
        + "<h3>Clubnaam in berichten</h3>"
        + form(
            "club_name",
            '<label for="club_name">Naam van de eigen club in berichten (leeg = naam uit Voetbalnederland)</label>'
            f'<input type="text" id="club_name" name="club_name" value="{esc(settings.club_name)}" '
            'maxlength="60" autocomplete="off" placeholder="bv. VSCO&#x27;61">',
            "Clubnaam opslaan",
        )
        + "<h3>Interval</h3>"
        + form(
            "poll",
            f'<label for="poll">Elke zoveel seconden bevragen ({MIN_POLL_SECONDS}-{MAX_POLL_SECONDS})</label>'
            f'<input type="number" id="poll" name="poll" class="num" min="{MIN_POLL_SECONDS}" '
            f'max="{MAX_POLL_SECONDS}" step="1" inputmode="numeric" required value="{esc(settings.poll_seconds)}">',
            "Interval opslaan",
        )
        + "<h3>Adres van de API</h3>"
        + form(
            "voetbalnederland_url",
            '<label for="voetbalnederland_url">Adres (https://); openbare API, geen sleutel nodig</label>'
            f'<input type="url" id="voetbalnederland_url" name="voetbalnederland_url" '
            f'value="{esc(settings.voetbalnederland_url)}" required maxlength="2048" autocomplete="off">',
            "Adres opslaan",
        )
    )


def logging_html(request: web.Request) -> str:
    live = live_logs(request.app)
    raw_lines = tail_lines(request.app["raw_log"], RAW_LOG_ENTRIES, max_bytes=3_000_000)
    return (
        '<h3>Berichten en fouten</h3>'
        f'<p id="live-note" class="muted small">{esc(live["note"])}</p>'
        f'<pre id="live-log">{esc(live["app"])}</pre>'
        '<h3>Ruwe websocketberichten</h3>'
        f'<p class="muted small">De laatste {RAW_LOG_ENTRIES}, bij het laden van de pagina. Tik op een bericht '
        "voor de wedstrijdstatus (<code>scoreboard_data</code>).</p>"
        f"{raw_entries_html(raw_lines)}"
    )


# Instellingenblok per bron: bron-id -> (blok-id, titel, inhoud(request, **fouten)).
# Alleen het blok van de gekozen bron staat op de pagina (of een blok met een fout of
# melding, bv. na een POST van een script).
# Logo van LED Scores (lokale kopie: de CSP laat alleen afbeeldingen van de eigen server toe).
# Het logo heeft witte delen, daarom staat het op een donker plaatje (ook in licht).
LEDSCORE_LOGO = (
    '<div class="brand-plate"><img src="/static/ledscores-logo.png" alt="LED Scores" width="300" height="59"></div>'
)


def scoreboard_panel(request: web.Request, connection: dict | None = None, goal_wait: dict | None = None) -> str:
    return (
        LEDSCORE_LOGO
        + connection_html(request, **(connection or {}))
        + "<h3>Wachten op de doelpuntmaker</h3>"
        + goal_wait_html(request, **(goal_wait or {}))
    )


SOURCE_PANELS: dict[str, tuple[str, str, Callable[..., str]]] = {
    "scoreboard": ("scorebord", "LED Scores", scoreboard_panel),
    "voetbalnederland": ("voetbalnederland", "Voetbalnederland", voetbalnederland_html),
}


def active_source_id(app: web.Application) -> str:
    manager = app.get("sources")
    if manager is not None:
        return manager.active_id
    chosen = app["settings"].source
    return chosen if chosen in SOURCE_PANELS else "scoreboard"


# Welk blok open moet na opslaan (via ?msg=...).
BLOCK_FOR_MESSAGE = {
    "source": "bron",
    "url": "scorebord", "token": "scorebord", "goal_wait": "scorebord",
    "team": "bron", "club_name": "voetbalnederland",
    "poll": "voetbalnederland", "voetbalnederland_url": "voetbalnederland",
    "hashtag": "hashtag", "hashtag_reset": "hashtag",
    "templates": "sjablonen", "reset": "sjablonen",
}
OPEN_HASH_JS = (
    "(function(){function o(){var d=location.hash&&document.getElementById(location.hash.slice(1));"
    "if(d&&d.tagName==='DETAILS'){d.open=true;d.scrollIntoView();}}o();"
    "window.addEventListener('hashchange',o);})();"
)


def app_page(
    request: web.Request,
    open_block: str | None = None,
    connection: dict | None = None,
    goal_wait: dict | None = None,
    templates: tuple[dict, dict] | None = None,
    status: int = 200,
    voetbalnederland: dict | None = None,
    hashtag: dict | None = None,
    source: dict | None = None,
) -> web.Response:
    """Connectie (alleen beheerder): uitklapbare blokken, elk met een eigen opslaanknop.

    Fouten in de blokken Hashtag en Sjablonen tonen de pagina Sjablonen."""
    if open_block in TEMPLATE_BLOCKS:
        return templates_page(request, templates=templates, hashtag=hashtag, status=status)
    # Zonder melding staat het blok Bron open: dat is de belangrijkste keuze.
    open_block = open_block or BLOCK_FOR_MESSAGE.get(request.query.get("msg", ""), "bron")
    block = block_maker(open_block)

    panel_args = {
        "scoreboard": {"connection": connection, "goal_wait": goal_wait},
        "voetbalnederland": voetbalnederland or {},
    }
    active = active_source_id(request.app)
    panels = ""
    for source_id, (block_id, title, render) in SOURCE_PANELS.items():
        if source_id == active or block_id == open_block:
            panels += block(block_id, title, render(request, **panel_args.get(source_id, {})))

    body = (
        f"{flash(request)}"
        '<p class="note">Deze instellingen gelden voor iedereen die de app gebruikt.</p>'
        + block("bron", "Bron en club", source_html(request, **(source or {})))
        + panels
        + block("status", "Status", f'<div id="live-table">{status_table_html(request.app)}</div>')
        + block("logging", "Logging", logging_html(request))
    )
    response = page(
        request, "Connectie", "/app", body, script=LIVE_JS + REVEAL_JS + OPEN_HASH_JS + TEAM_JS, aside=led_html(request)
    )
    response.set_status(status)
    return response


def block_maker(open_block: str | None):
    def block(block_id: str, title: str, content: str, is_open: bool = False) -> str:
        opened = " open" if is_open or block_id == open_block else ""
        return (
            f'<details class="block" id="{block_id}"{opened}><summary>{esc(title)}</summary>'
            f'<div class="block-body">{content}</div></details>'
        )

    return block


# Blokken op de pagina Sjablonen (de rest staat op Connectie).
TEMPLATE_BLOCKS = ("hashtag", "sjablonen")


def page_for_block(block_id: str) -> str:
    return "/sjablonen" if block_id in TEMPLATE_BLOCKS else "/app"


def templates_page(
    request: web.Request,
    templates: tuple[dict, dict] | None = None,
    hashtag: dict | None = None,
    status: int = 200,
) -> web.Response:
    """Sjablonen (alleen beheerder): de hashtag en de teksten van alle berichten."""
    settings: Settings = request.app["settings"]
    values, errors = templates or (settings.templates, {})
    block = block_maker(None)
    body = (
        f"{flash(request)}"
        + block("hashtag", "Hashtag", hashtag_html(request, **(hashtag or {})), is_open=True)
        + block("sjablonen", "Berichtteksten", templates_form(request, values, errors), is_open=True)
    )
    response = page(request, "Sjablonen", "/sjablonen", body, script=LIVE_JS + OPEN_HASH_JS, aside=led_html(request))
    response.set_status(status)
    return response


async def templates_view(request: web.Request) -> web.Response:
    return templates_page(request)


async def app_view(request: web.Request) -> web.Response:
    return app_page(request)


def redirect_to(target: str):
    async def handler(request: web.Request) -> web.Response:
        raise web.HTTPSeeOther(target)

    return handler


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
        elif action == "source" and form.get("search"):
            # Zoeken zonder JavaScript: gekozen bron blijft staan, nog niets opgeslagen.
            return await team_search(request, str(form.get("q", "")), chosen=str(form.get("source", "")))
        elif action == "source":
            save_source_and_club(request, form)
        elif action == "team_search":
            return await team_search(request, str(form.get("q", "")))
        elif action == "team":
            if "team" in form:  # keuzelijst: "id:naam"
                team_id, _, name = str(form.get("team", "")).partition(":")
            else:
                team_id, name = form.get("team_id", ""), str(form.get("team_name", ""))
            settings.set_team(team_id, name)
            logger.info("Team voor Voetbalnederland gewijzigd naar %s", settings.team_name or settings.team_id)
        elif action == "club_name":
            settings.set_club_name(str(form.get("club_name", "")))
        elif action == "poll":
            settings.set_poll_seconds(form.get("poll", ""))
        elif action == "voetbalnederland_url":
            settings.set_voetbalnederland_url(str(form.get("voetbalnederland_url", "")))
            logger.info("Adres van de Voetbalnederland-bron gewijzigd via de webomgeving")
        elif action == "hashtag":
            settings.set_hashtag(form.get("hashtag", ""))
            logger.info("Hashtag voor de wedstrijd gewijzigd naar %s", settings.hashtag_override or "(standaard)")
        elif action == "hashtag_reset":
            settings.clear_hashtag()
            logger.info("Hashtag hersteld naar de standaard")
        else:
            raise ValueError("Onbekende actie.")
    except ValueError as error:
        if action == "source":
            failed = {"error": str(error), "chosen": str(form.get("source", ""))}
            return app_page(request, "bron", source=failed, status=400)
        if action == "team":
            return app_page(request, "bron", source={"team": {"error": str(error)}}, status=400)
        if action in ("club_name", "poll", "voetbalnederland_url"):
            failed = {"error": str(error), "error_field": action}
            return app_page(request, "voetbalnederland", voetbalnederland=failed, status=400)
        if action == "token":
            return app_page(request, "scorebord", connection={"token_error": str(error)}, status=400)
        if action == "goal_wait":
            wait = {"wait_error": str(error), "wait_value": str(form.get("goal_wait", ""))}
            return app_page(request, "scorebord", goal_wait=wait, status=400)
        if action == "hashtag":
            failed = {"hashtag_error": str(error), "hashtag_value": str(form.get("hashtag", ""))}
            return app_page(request, "hashtag", hashtag=failed, status=400)
        url = {"url_error": str(error), "url_value": str(form.get("url", ""))}
        return app_page(request, "scorebord", connection=url, status=400)
    block_id = BLOCK_FOR_MESSAGE[action]
    raise web.HTTPSeeOther(f"{page_for_block(block_id)}?msg={action}#{block_id}")


async def find_teams(request: web.Request, query: str) -> list[dict]:
    """Zoek clubs bij Voetbalnederland. ValueError bij een te korte zoekterm of zonder bron."""
    query = " ".join(query.split())[:60]
    manager = request.app.get("sources")
    source = manager.sources.get("voetbalnederland") if manager is not None else None
    if len(query) < 2:
        raise ValueError("Vul minstens 2 letters in.")
    if source is None:
        raise ValueError("Voetbalnederland is niet beschikbaar.")
    return (await source.client.search_clubs(query))[:TEAM_RESULTS]


def save_source_and_club(request: web.Request, form) -> None:
    """Bron en club samen opslaan. Bij Voetbalnederland is een club verplicht."""
    settings: Settings = request.app["settings"]
    manager = request.app.get("sources")
    chosen = str(form.get("source", ""))
    if manager is None or chosen not in manager.sources:
        raise ValueError("Kies een van de bronnen.")
    team = str(form.get("team", "") or "")
    if chosen == "voetbalnederland":
        if team:
            team_id, _, name = team.partition(":")
            settings.set_team(team_id, name)
        elif not settings.team_id:
            raise ValueError("Kies je club bij Voetbalnederland.")
    changed = chosen != settings.source
    if changed:  # alleen bij een wissel de bron herstarten
        settings.set_source(chosen)
    logger.info(
        "Bron %s: %s, club %s", "gewijzigd" if changed else "opgeslagen",
        manager.sources[chosen].label, manager.sources[chosen].club() or "-",
    )


async def team_search(request: web.Request, query: str, chosen: str | None = None) -> web.Response:
    """Zoeken zonder JavaScript: de pagina met de resultaten in het blok Bron en club."""
    def page_with(team: dict, status: int = 200) -> web.Response:
        source = {"team": {"query": query, **team}, "chosen": chosen or "voetbalnederland"}
        return app_page(request, "bron", source=source, status=status)

    try:
        results = await find_teams(request, query)
    except ValueError as error:
        return page_with({"error": str(error)}, 400)
    except VoetbalnederlandError as error:
        return page_with({"error": str(error)}, 502)
    return page_with({"results": results})


async def teams_json(request: web.Request) -> web.Response:
    """Live zoeken: clubs als JSON ([{id, name, detail}]); fouten als lege lijst met een status."""
    try:
        results = await find_teams(request, request.query.get("q", ""))
    except ValueError:
        return web.json_response([], status=400)
    except VoetbalnederlandError:
        return web.json_response([], status=502)
    out = []
    for club in results:
        name, detail = team_label(club)
        out.append({"id": club.get("id"), "name": name, "detail": detail})
    return web.json_response(out)


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
        '<details class="intro"><summary>Aanduidingen</summary>'
        '<p class="muted small">Gebruik deze in een sjabloon, bijvoorbeeld <code>{thuis} - {uit} {stand}</code>. '
        "Een leeg sjabloon betekent dat dat bericht niet wordt verstuurd.</p>"
        f"<table>{legend}</table></details>"
        '<form method="post" action="/templates">'
        f'{csrf_field(request)}{"".join(fields)}'
        '<div class="row"><button type="submit">Sjablonen opslaan</button></div></form>'
        '<form method="post" action="/templates/reset" class="row">'
        f'{csrf_field(request)}<button type="submit" class="secondary">Standaardteksten herstellen</button></form>'
    )


async def templates_post(request: web.Request) -> web.Response:
    form = request["form"]
    settings: Settings = request.app["settings"]
    submitted = {
        key: str(form.get(f"tpl_{key}", settings.templates[key])).strip() for key in TEMPLATE_INFO
    }
    errors = {key: error for key in TEMPLATE_INFO if (error := validate_template(key, submitted[key]))}
    if errors:
        return app_page(request, "sjablonen", templates=(submitted, errors), status=400)
    settings.set_templates(submitted)
    logger.info("Berichtsjablonen gewijzigd via de webomgeving")
    raise web.HTTPSeeOther("/sjablonen?msg=templates#sjablonen")


async def templates_reset(request: web.Request) -> web.Response:
    request.app["settings"].reset_templates()
    logger.info("Berichtsjablonen hersteld naar de standaard via de webomgeving")
    raise web.HTTPSeeOther("/sjablonen?msg=reset#sjablonen")


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
            f'data-text="{esc(item["share"]).replace(chr(10), "&#10;")}" '
            f'data-fallback="{esc(item["url"])}"{photo_attr}>'
            f"{thumb}"
            f'<div class="msg-body"><div class="msg-meta"><span>{esc(item["time"])}</span>{counter}</div>'
            f'<p class="msg-text">{esc(item["text"])}</p></div>'
            f'<div class="msg-actions"><button type="button" class="share">{SHARE_ICON}Delen</button>{toggle}</div>'
            '<p class="item-hint" role="status"></p></li>'
        )
    return f'<ul class="msgs">{"".join(items)}</ul>'


def share_text(text: str) -> str:
    """Tekst om te delen: eindigt met een enter, zodat X na een hashtag aan het eind
    niet meteen het hashtag-venster opent."""
    return text.rstrip("\n") + "\n"


def message_item(message: MatchMessage) -> dict:
    shared = share_text(message.text)
    return {
        "time": message.moment.astimezone().strftime("%H:%M:%S"),
        "text": message.text,
        "share": shared,
        "url": post_url(shared),
        "length": len(shared),
        "photo": f"/photo/{photo_id(message.image)}" if message.image else None,
        # Vaste sleutel per bericht: foto-vinkje blijft staan, en de link in een pushmelding.
        "key": message.key,
    }


def live_logs(app: web.Application, admin: bool = True) -> dict:
    """Live-gegevens. Een gebruiker (geen beheerder) krijgt geen logregels en geen statustabel."""
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
    data = {
        "messages": messages,
        "messages_html": messages_html(messages),
        "led": dict(zip(("level", "status"), led_state(app))),
    }
    if admin:
        data["status_html"] = status_table_html(app)
        data["app"] = "\n".join(reversed(app_lines)) or "(nog geen logregels)"
        data["note"] = note
    return data


async def logs_data(request: web.Request) -> web.Response:
    push = request.app["push"]
    endpoint = request.headers.get("X-Push-Endpoint")
    if push is not None and endpoint:
        push.store.mark_present(endpoint)  # startpagina open: geen pushmeldingen naar dit apparaat
    return web.json_response(live_logs(request.app, admin=request.get("role") == ROLE_ADMIN))


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


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "connection": request.app["state"].status})


# --- installeerbare webapp ---------------------------------------------------

SERVICE_WORKER_JS = """// Service worker: maakt de app installeerbaar. Er wordt niets opgeslagen
// (de pagina's bevatten persoonlijke gegevens); zonder netwerk volgt een melding.
const OFFLINE = '<!doctype html><html lang="nl"><meta charset="utf-8">'
  + '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Geen verbinding</title>'
  + '<body style="font:16px system-ui,sans-serif;padding:24px">'
  + '<h1>Geen verbinding</h1><p>MatchShareNL is niet bereikbaar. Controleer je netwerk en probeer het opnieuw.</p>';
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
  event.waitUntil(self.registration.showNotification(data.title || 'MatchShareNL', {
    body: data.body || '', tag: data.tag || undefined,
    icon: data.icon || '/static/icon-192.png', badge: data.badge || '/static/badge-logo.png',
    data: {url: data.url || '/'},
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
        "name": "MatchShareNL",
        "short_name": "MatchShareNL",
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
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
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


def role_credentials(app, role: str) -> tuple[str, str] | None:
    if role == ROLE_ADMIN:
        return app["username"], app["password"]
    if role == ROLE_USER and app.get("share_password"):
        return app["share_user"], app["share_password"]
    return None


def _session_signature(app, role: str, payload: str) -> str | None:
    # De sleutel hangt af van de gebruikersnaam en het wachtwoord van die rol: wijzig je
    # die, dan zijn de bestaande sessies van die rol direct ongeldig.
    credentials = role_credentials(app, role)
    if credentials is None:
        return None
    key = hmac.new(
        app["session_secret"], f"{role}\0{credentials[0]}\0{credentials[1]}".encode(), hashlib.sha256
    ).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session(app, now: float | None = None, role: str = ROLE_ADMIN) -> str:
    expires = int((time.time() if now is None else now) + app["session_days"] * 86400)
    payload = f"{role}:{expires}"
    return f"{payload}.{_session_signature(app, role, payload)}"


def session_role(app, value: str | None, now: float | None = None) -> str | None:
    """De rol uit een geldig sessiecookie, of None."""
    if not value or "." not in value:
        return None
    payload, signature = value.rsplit(".", 1)
    role, _, expires = payload.partition(":")
    expected = _session_signature(app, role, payload)
    if expected is None or not hmac.compare_digest(signature, expected):
        return None
    try:
        return role if int(expires) > (time.time() if now is None else now) else None
    except ValueError:
        return None


def valid_session(app, value: str | None, now: float | None = None) -> bool:
    return session_role(app, value, now) is not None


def credentials_role(app, username: str, password: str) -> str | None:
    """Welke rol hoort bij deze gebruikersnaam en dit wachtwoord (beheerder gaat voor)."""
    for role in (ROLE_ADMIN, ROLE_USER):
        credentials = role_credentials(app, role)
        if credentials is None:
            continue
        user_ok = hmac.compare_digest(username.encode(), credentials[0].encode())
        pass_ok = hmac.compare_digest(password.encode(), credentials[1].encode())
        if user_ok and pass_ok:
            return role
    return None


def basic_role(request: web.Request) -> str | None:
    """HTTP Basic, voor scripts en tools (de browser gebruikt de inlogpagina)."""
    try:
        auth = BasicAuth.decode(request.headers.get("Authorization", ""), encoding="utf-8")
    except ValueError:
        return None
    return credentials_role(request.app, auth.login, auth.password)


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
    if session_role(request.app, request.cookies.get(SESSION_COOKIE)):
        raise web.HTTPSeeOther(safe_next(request.query.get("next")))
    form = login_form(request, next_url=safe_next(request.query.get("next")))
    return page(request, "Inloggen", "", form, show_nav=False)


async def login_post(request: web.Request) -> web.Response:
    form = request["form"]
    app = request.app
    next_url = safe_next(str(form.get("next", "/")))
    role = credentials_role(app, str(form.get("username", "")), str(form.get("password", "")))
    if role is None:
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
        make_session(app, role=role),
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
    role = session_role(app, request.cookies.get(SESSION_COOKIE))
    if not public and role is None:
        if "Authorization" in request.headers:
            role = basic_role(request)
            if role is None:
                await asyncio.sleep(1)  # remt het raden van wachtwoorden
                return web.Response(status=401, text="Onjuiste inloggegevens")
        elif wants_html(request):
            raise web.HTTPSeeOther(f"/login?{urlencode({'next': request.path_qs})}")
        else:
            return web.Response(status=401, text="Inloggen vereist")
    request["role"] = role
    if request.path in ADMIN_PATHS and role != ROLE_ADMIN:
        if wants_html(request):
            raise web.HTTPSeeOther("/")
        return web.Response(status=403, text="Alleen voor beheerders.")
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
    share_user: str = "gebruiker",
    share_password: str = "",
    sources=None,
) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app["push"] = push  # PushSender, of None als meldingen uit staan
    app["sources"] = sources  # SourceManager (de bronnen), of None
    app["fetch_photo"] = photo_fetcher or fetch_photo  # vervangbaar in tests
    app["photo_cache"] = OrderedDict()
    app["settings"] = settings
    app["state"] = state
    app["app_log"] = app_log
    app["raw_log"] = raw_log
    app["memory_log"] = memory_log
    app["username"] = username
    app["password"] = password
    # Tweede login met alleen delen en eigen instellingen; leeg wachtwoord = uit.
    app["share_user"] = share_user
    app["share_password"] = share_password
    app["csrf"] = secrets.token_urlsafe(32)
    app["session_secret"] = session_secret or secrets.token_bytes(32)
    app["session_days"] = session_days
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/login", login_page),
            web.post("/login", login_post),
            web.post("/logout", logout_post),
            web.post("/theme", theme_post),
            web.get("/manifest.webmanifest", manifest),
            web.get("/sw.js", service_worker),
            web.static("/static", STATIC_DIR, append_version=False),
            web.get("/", messages_page),
            web.get("/pushmeldingen", push_page),
            web.get("/account", account_page),
            web.get("/me", redirect_to("/pushmeldingen")),  # oud adres
            web.get("/app", app_view),
            web.get("/sjablonen", templates_view),
            web.post("/settings", settings_post),
            web.get("/teams", teams_json),
            web.post("/templates", templates_post),
            web.post("/templates/reset", templates_reset),
            # Oude adressen sturen door naar het juiste blok.
            web.get("/status", redirect_to("/app#status")),
            web.get("/settings", redirect_to("/app#scorebord")),
            web.get("/templates", redirect_to("/sjablonen")),
            web.get("/logs", redirect_to("/app#logging")),
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
    share_user: str = "gebruiker",
    share_password: str = "",
    sources=None,
) -> web.AppRunner:
    app = create_web_app(
        settings, state, app_log, raw_log, username, password, memory_log,
        session_secret=session_secret, session_days=session_days, push=push,
        share_user=share_user, share_password=share_password, sources=sources,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Webomgeving bereikbaar op http://%s:%s", host, port)
    return runner
