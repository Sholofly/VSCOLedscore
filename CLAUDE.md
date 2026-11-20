# CLAUDE.md

Python 3.13-app die verbindt met de VSCO-scorebord-websocket (ledscores),
wedstrijdgebeurtenissen herkent en die als berichten toont in een webomgeving
voor de telefoon (met een Delen-knop en optioneel de foto van de doelpuntmaker). Draait in Docker op een Synology-NAS.
Gebruikersdocumentatie staat in [README.md](README.md).

## Afspraken met de gebruiker

- Communiceer in het Nederlands; alle teksten in de app zijn ook Nederlands.
- **Toon nooit het token of `WEB_PASSWORD`**, schrijf ze niet in code, tests of
  commits en **wijzig `.env` niet**. Controleer zo nodig alleen óf iets is
  ingesteld (bv. `grep -c '^WEB_PASSWORD=.' .env`), nooit de waarde.
- Geen X/Twitter-koppeling of -login in de app: posten gaat via de intent-link
  en het deelmenu van de telefoon.
- Houd wijzigingen klein, voeg gerichte tests toe bij nieuw gedrag en draai de
  tests na elke wijziging. Lees eerst de bestaande code.
- Na een afgeronde wijziging: committen en pushen naar `main`
  (github.com/Sholofly/VSCOLedscore); GitHub Actions bouwt dan de image.

## Opbouw

| Bestand | Inhoud |
|---|---|
| [app.py](app.py) | `MatchLifecycle` (berichten uit statusovergangen), `Connector` (verbinding, reconnect), logging, `main()` |
| [messages.py](messages.py) | Sjablonen: standaardteksten, aanduidingen (`{minuut}` ...), validatie, `Message` (tekst + foto-URL) |
| [settings.py](settings.py) | URL/token/sjablonen in `data/settings.json` (heeft voorrang op `.env`), atomair opgeslagen |
| [state.py](state.py) | `ConnectionState` (status + laatste wedstrijdberichten), `MemoryLogHandler` |
| [web.py](web.py) | aiohttp-webomgeving: pagina's, inloggen/sessies, live-updates, foto-doorgifte, PWA |
| `static/` | App-iconen (PNG) |
| `test_*.py` | unittest; `test_connector.py` gebruikt een echte lokale websocketserver |

## Commando's

```powershell
python -m unittest                      # alle tests (~10 s)
python -m unittest test_web             # één bestand
python app.py                           # lokaal; leest .env
docker build -t vsco-scorebord-connector .
docker compose up -d --no-build         # lokaal draaien met die image
```

- `docker compose up --build` faalt op deze Windows-machine met "failed to
  execute bake": bouw met `docker build` en start met `--no-build`.
- Draai tests met een time-out (bv. `timeout 90 python -m unittest`), zodat een
  hangende test opvalt.
- Op de NAS: `git pull && docker compose pull && docker compose up -d`
  (`IMAGE=ghcr.io/sholofly/vscoledscore:latest` en `BIND_ADDRESS=0.0.0.0` in
  de `.env` daar).

## Het scorebordbericht (vastgesteld uit echte berichten)

Elk bericht is de volledige status (~175 KB JSON). Relevant in `scoreboard_data`:

- `startedAt` / `endedAt`: gevuld bij start / einde. Een correctie kan ze weer leegmaken.
- `rest`: `true` tijdens rust; begin tweede helft = `rest` van true naar false.
- `time`: seconden **binnen de helft**; begint in de tweede helft opnieuw bij 0
  en staat tijdens rust op 0. `period`: 0 = eerste helft, 1 = tweede helft.
- `scoreboard.home` / `.away`: stand. Een correctie verlaagt de stand.
- `teams.home` / `.away`: team-id's; namen staan in de bovenliggende `teams`-lijst
  (`scoreboard_name`). De thuisploeg is de eigen club.
- `scorers.home[n]` / `.away[n]`: `{"player": {"name", "image", "goalImage", ...}, "time", "period"}`.
  De lijst kan `null` bevatten. **De doelpuntmaker komt ~5 s ná de standwijziging,
  in een apart bericht**; zonder gekozen speler komt er niets.

## Gedrag dat bewust zo is

- De eerste ontvangen status is de beginsituatie: geen meldingen achteraf.
- Eigen doelpunt wacht tot 20 s op de doelpuntmaker (instelbaar op de pagina
  Verbinding, `Settings.goal_wait_seconds`; per doelpunt opgevraagd) en
  wordt dan als één bericht gemeld; doelpunt tegenstander direct.
- Stand omlaag = correctie: wachtend doelpunt vervalt, geen melding.
- Minuten met blessuretijd per helft: eerste helft boven 45 wordt `45+n`, tweede
  helft boven 90 wordt `90+n` (`_minute_label`, op basis van `REGULAR_MINUTES`).
- Bij een reconnect blijft de wedstrijdstatus bewaard; bij een andere URL niet.
- `AFGELOPEN!` in hoofdletters alleen bij winst; blessuretijd = minuten boven
  `REGULAR_MINUTES`.
- Een leeg sjabloon betekent: dat bericht niet versturen.

## Beveiliging (niet afzwakken zonder overleg)

- Inloggen via de inlogpagina met een ondertekend sessiecookie (HttpOnly,
  SameSite=Lax, 30 dagen); HTTP Basic blijft voor scripts. Openbaar zijn alleen
  `/login`, `/health`, `/manifest.webmanifest`, `/sw.js` en `/static/`.
- CSRF-token op alle formulieren; CSP met een nonce per pagina; alles escapen.
- Het token staat alleen op de pagina Verbinding (verborgen veld met oog-icoon)
  en wordt uit alle logregels gehaald (`TokenRedactingFilter`).
- `/photo/<id>` geeft alleen foto's door die in een wedstrijdbericht voorkomen
  (geen willekeurige URL's ophalen).
- De service worker slaat niets op (pagina's bevatten persoonsgegevens).
- `logs/` (ruwe berichten met spelersnamen), `data/` en `.env` staan in
  `.gitignore`; zet nooit echte spelersnamen of tokens in tests of commits.

## Webomgeving

- Gemaakt voor de telefoon: grote tikvlakken (min. 44 px), navigatie op één
  regel, controleer opmaak op ~390 px breed. Een schermafbeelding kan met Edge
  zonder venster (`msedge --headless=new --screenshot`); Edge heeft een minimale
  vensterbreedte van ~500 px, dus zet de pagina zelf op `width:390px`.
- Pagina's: `/` berichten + statuslampje, `/status`, `/settings` (Verbinding),
  `/templates` (Sjablonen), `/logs`. Live-updates via `/logs/data` (JSON).

## Valkuilen die al eens fout gingen

- `contextlib.suppress(asyncio.CancelledError)` rond het afwachten van een taak
  slikt ook het stopsignaal van de eigen taak in, waarna de lus eindeloos
  doorloopt. Gebruik `asyncio.gather(..., return_exceptions=True)`.
- aiohttp `response.content.read(n)` geeft alleen wat er al binnen is, niet de
  hele body: lees met `iter_chunked` tot het einde en controleer de lengte.
- Op Windows duurt verbinden met een gesloten poort ~2 s; injecteer in tests
  een falende `connect_func` in plaats van een echte gesloten poort.
- Bij timinggevoelige fouten: controleer dat een regressietest de oude code
  echt laat falen (bv. de oude versie via `git show HEAD:bestand` in een
  scratchmap testen).
- Een CSS-escape zoals backslash-25B8 in een gewone Python-string wordt een
  octale escape (stuurteken). Gebruik geen backslash-escapes in CSS/JS-strings
  of gebruik een getekend alternatief; een test controleert op stuurtekens.
- Patchen via een bash-heredoc maakt van backslash-n soms een echte regelovergang
  in de code. Schrijf zulke patches liever via de editor of een scriptbestand.
- Zonder internet faalt een verbinding niet, er komt alleen niets meer door. Server:
  ping (`PING_INTERVAL`/`PING_TIMEOUT`) plus een korte `CLOSE_TIMEOUT`, anders wacht
  websockets nog 10 s op een afscheid dat nooit komt. Pagina: elke fetch krijgt een
  time-out (AbortController) en het lampje gaat uit zonder recente update; een
  hangende fetch op een telefoon faalt anders pas na minuten.
- Gedrag in een echte browser controleren kan met Edge zonder venster plus het
  DevTools-protocol (`--remote-debugging-port`, `Runtime.evaluate` via websockets).
- Op de NAS kan `./logs` onschrijfbaar zijn (uid 1000); de app logt dan alleen
  naar console en geheugen, en de webomgeving valt daarop terug.
