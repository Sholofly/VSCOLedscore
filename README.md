# MatchShareNL

MatchShareNL maakt van wedstrijdinformatie kant-en-klare berichten om te delen:
aftrap, doelpunten (met doelpuntmaker en foto), rust, tweede helft en einde. De
informatie komt van een **bron** die je kiest: het **scorebord** van de club of
**Voetbalnederland**; er kunnen later bronnen bij komen. De berichten verschijnen
in een webapp voor de telefoon, met een Delen-knop en pushmeldingen. Daar beheer
je ook de bron, de berichtteksten en bekijk je de logging.

## Instellen (.env)

Kopieer `.env.example` naar `.env` (dat bestand staat in `.gitignore`):

```dotenv
WS_URL=wss://wss.ledscores.score.tel/clubs/vsco61/
WS_TOKEN=JWT <jouw-geldige-token>
REGULAR_MINUTES=90
WEB_USER=admin
WEB_PASSWORD=<kies-een-wachtwoord>
# optioneel, voor anderen die alleen berichten delen:
SHARE_USER=gebruiker
SHARE_PASSWORD=<ander-wachtwoord>
```

- `WS_URL` en `WS_TOKEN` (de bron LED Scores) zijn beginwaarden. Wat je later in de webomgeving
  opslaat, wordt bewaard in `data/settings.json` (in Docker: het volume
  `scorebord-data`) en **heeft voorrang** op `.env`. Wijzig je daarna het token
  in `.env`, dan merk je daar niets van: gebruik dan de webomgeving.
- Het token wordt als `token`-queryparameter aan de URL toegevoegd en correct
  URL-gecodeerd.
- `WEB_PASSWORD` is verplicht in Docker. Lokaal geldt: zonder wachtwoord staat
  de webomgeving uit en moeten `WS_URL` en `WS_TOKEN` in `.env` staan. Tokens worden
  niet automatisch vernieuwd.

## Draaien met Docker

```powershell
docker compose up --build -d
docker compose logs -f
```

De webomgeving staat op <http://127.0.0.1:8080> (poort aanpasbaar met
`HOST_PORT` in `.env`) en is standaard alleen vanaf de machine zelf bereikbaar.
Wil je hem op je netwerk gebruiken (bijvoorbeeld op een NAS), zet dan
`BIND_ADDRESS=0.0.0.0` in `.env`, gebruik een sterk wachtwoord en open hem op
`http://<ip-van-de-machine>:<HOST_PORT>`. De verbinding is niet versleuteld, dus
zet er HTTPS voor als hij buiten je eigen netwerk komt.

De container draait niet als root, heeft een healthcheck (`/health`), start zelf
opnieuw (`unless-stopped`) en stopt netjes bij `docker stop`. De map `./logs`
bevat `app.log` en `websocket-raw.log`. Op Linux moet `./logs` schrijfbaar zijn
voor gebruiker-id 1000.

## Deployen op een NAS (via GitHub)

GitHub bouwt bij elke push naar `main` automatisch een image voor `amd64` en
`arm64` ([workflow](.github/workflows/docker.yml), eerst draaien de tests) en
zet die op `ghcr.io/<jouwnaam>/<repo>:latest`. De NAS haalt alleen de image op.

Eenmalig op de NAS (via SSH), met `<jouwnaam>/<repo>` van je GitHub-repo:

```sh
git clone https://github.com/<jouwnaam>/<repo>.git && cd <repo>
cp .env.example .env
nano .env          # vul WS_TOKEN, WEB_PASSWORD en IMAGE=ghcr.io/<jouwnaam>/<repo>:latest in
mkdir -p logs && chown 1000:1000 logs
docker compose pull
docker compose up -d
```

Bijwerken na een nieuwe versie:

```sh
git pull && docker compose pull && docker compose up -d
```

- **Eenmalig na de naamswijziging naar MatchShareNL:** de service heet nu
  `matchshare` (was `websocket-logger`). Gebruik één keer
  `docker compose up -d --remove-orphans`, zodat de oude container verdwijnt.
  Het volume `scorebord-data` houdt bewust zijn naam: je instellingen, sessies
  en meldingen blijven bewaard. Kies daarna onder Connectie > Bron en club de bron.

- **Wacht tot de image klaar is.** Na elke push bouwt GitHub de image; dat duurt
  een paar minuten. Pull je te vroeg, dan krijg je nog de oude versie. Kijk bij
  het tabblad Actions van de repo of de laatste run groen is.
- **Controleren:** `docker compose ps` moet `healthy` tonen en
  `docker compose logs --tail 20` moet `Webomgeving bereikbaar` en de gekozen
  bron (`Bron: ...`) bevatten.
- **Je instellingen blijven bewaard:** `.env` staat niet in git en de bron, de
  URL, het token en de sjablonen uit de webomgeving staan in het volume
  `scorebord-data`.
- **`git pull` klaagt over lokale wijzigingen?** Dan is een bestand in de map
  aangepast, vaak `compose.yaml`. Zet instellingen altijd in `.env`. Met
  `git status` zie je welk bestand het is.
- Oude, ongebruikte images ruim je op met `docker image prune`.

- **Privé-repo:** de image is dan ook privé. Maak op GitHub een token met alleen
  `read:packages` en log eenmalig in: `echo <token> | docker login ghcr.io -u <jouwnaam> --password-stdin`.
  Voor `git clone` gebruik je hetzelfde soort token of een deploy key. Je kunt
  het pakket ook publiek maken (Packages, Package settings).
- **`.env` komt nooit in GitHub:** het staat in `.gitignore`, net als `logs/` en
  `data/`. Zet je token dus alleen op de NAS, of in de webomgeving.
- Kan de NAS `./logs` niet schrijven, dan start de app toch en logt hij alleen
  naar de console (en toont hij een waarschuwing).
- De webomgeving is standaard alleen bereikbaar vanaf de NAS zelf. Wil je er
  vanaf je pc bij, zet dan `BIND_ADDRESS=0.0.0.0` in `.env` (met `HOST_PORT` voor
  een andere poort), kies een sterk wachtwoord en herstart met
  `docker compose up -d`. Blokkeert de firewall van de NAS de poort, open hem
  dan daar. Zet er HTTPS voor als je hem buiten je eigen netwerk gebruikt.

## Lokaal draaien

Vereist Python 3.13 of nieuwer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Met `WEB_PASSWORD` in `.env` is de webomgeving bereikbaar op
<http://127.0.0.1:8080> (`WEB_HOST` en `WEB_PORT` zijn aanpasbaar). Stoppen kan
met `Ctrl+C`.

## Webomgeving

De webomgeving is gemaakt voor de telefoon. **Berichten** is altijd de basis.
Onderaan staat altijd een footer met links het logo (terug naar Berichten), in
het midden de naam en rechts het menu (hamburgerknop). Elke andere pagina opent
als paneel over Berichten en sluit je met het kruisje rechtsboven (of Escape).
Er zijn twee rollen:

| Rol | Inloggen met | Ziet |
|---|---|---|
| Beheerder | `WEB_USER` / `WEB_PASSWORD` | Berichten · Connectie · Sjablonen · Pushmeldingen · Account |
| Gebruiker | `SHARE_USER` / `SHARE_PASSWORD` | Berichten · Pushmeldingen · Account |

De gebruikerslogin is bedoeld voor anderen die berichten delen; zonder
`SHARE_PASSWORD` staat hij uit. Een gebruiker kan niets aan de app wijzigen (de
server weigert dat ook, niet alleen de schermen).

Je blijft 30 dagen ingelogd op een apparaat (`SESSION_DAYS`), ook na een
herstart of update. Wijzig je de naam of het wachtwoord van een rol, dan moet
alleen die rol opnieuw inloggen. Scripts en tools kunnen ook HTTP Basic
gebruiken. Formulieren hebben een CSRF-token, scripts zijn alleen met een nonce
per pagina toegestaan en alle uitvoer wordt ge-escaped.

- **Berichten (startpagina):** alleen de wedstrijdberichten, nieuwste bovenaan,
  met rechtsboven een statuslampje (groen = verbonden, oranje = bezig of
  wachten, rood = fout, grijs = de app is vanaf je telefoon niet bereikbaar).
  Valt het internet van de NAS weg, dan ziet de app dat binnen ongeveer 20
  seconden. De uitleg staat achter "Hoe werkt delen?".
  - **Delen:** bij elk bericht. Opent het deelmenu van je telefoon; kies daar X.
    Zonder deelmenu (op een pc, of zonder HTTPS) opent X met de tekst al
    ingevuld. De app heeft geen toegang tot je X-account nodig.
    De gedeelde tekst eindigt met een enter, zodat X na de hashtag niet
    meteen het hashtag-venster opent.
  - **Foto meesturen:** bij een eigen doelpunt met doelpuntmaker staat een
    preview van de foto (`goalImage`, anders `image`) en dit vinkje, standaard
    aan. Uitgezet wordt de preview grijs en deel je alleen de tekst. Delen met
    foto werkt alleen op een telefoon of tablet en **alleen via HTTPS**. De app
    haalt de foto zelf op en geeft alleen foto's door die in een
    wedstrijdbericht voorkomen. Let op: het gaat om foto's van spelers; of ze op
    X mogen, bepaalt de club.
  - Berichten boven 280 tekens worden gemarkeerd.
- **Pushmeldingen** (per gebruiker en apparaat): aan/uit met een testmelding.
- **Account:** de weergave (Systeem, Licht of Donker; Systeem volgt je apparaat,
  per apparaat bewaard), met welke rol je bent ingelogd, en uitloggen.
- **Connectie** (adres `/app`; alleen beheerder, geldt voor iedereen).
  Uitklapbare blokken, elk met een eigen opslaanknop; van de bronnen zie je
  alleen het blok van de gekozen bron:
  - **Bron en club** (staat bovenaan en open): kies de bron en je club en sla
    ze samen op met Opslaan. Bij LED Scores ligt de club vast (de thuisploeg);
    bij Voetbalnederland typ je een clubnaam en tik je de club aan in de
    resultaten die tijdens het typen verschijnen.
  - **LED Scores:** het logo van LED Scores, URL en token, en de wachttijd op de doelpuntmaker (standaard
    20 seconden, 0 = niet wachten). Het token staat verborgen in het veld en
    maak je zichtbaar met het oog-icoon; het staat alleen hier. Een nieuwe URL
    of een nieuw token bouwt de verbinding direct opnieuw op. De URL mag geen
    `token=` bevatten.
  - **Voetbalnederland:** clubnaam in berichten,
    interval en het adres van de API.
  - **Status:** de gekozen bron en zijn status (bij het scorebord: verbinding,
    laatste bericht en fout, URL en token met vervaldatum; bij Voetbalnederland:
    team, wedstrijd van vandaag, laatst bevraagd). Het lampje linkt hierheen.
  - **Logging:** de laatste 300 regels van `app.log` (live, nieuwste bovenaan)
    en de laatste 10 ruwe berichten met een samenvatting; uitgeklapt zie je de
    wedstrijdstatus (`scoreboard_data`) als ingesprongen JSON.

  De oude adressen `/status`, `/settings`, `/templates` en `/logs` sturen door
  naar het juiste blok.
- **Sjablonen** (adres `/sjablonen`; alleen beheerder, geldt voor iedereen):
  - **Hashtag:** een eigen hashtag voor de wedstrijd van je club die bezig is,
    anders de eerstvolgende; het blok toont welke wedstrijd dat is (zie hieronder).
  - **Berichtteksten:** de teksten van alle berichten, met voorbeeld. Een leeg
    sjabloon betekent dat dat bericht niet wordt verstuurd.

## Bronnen

Er draait altijd precies één bron: de bron die je kiest onder Connectie > Bron en club. Een
andere bron kiezen stopt de vorige direct. De berichten, sjablonen, hashtag,
meldingen en de Delen-knop werken voor elke bron hetzelfde.

- **LED Scores:** live via de websocket van het scorebord van LED Scores. Je club is de
  thuisploeg op het scorebord. Elk bericht heeft een speelminuut (met
  blessuretijd per helft); een eigen doelpunt wacht kort op de doelpuntmaker.
- **Voetbalnederland:** standen van het amateurvoetbal, door vrijwilligers
  bijgewerkt. Je club kies je onder Connectie > Bron en club,
  thuis of uit. De app haalt het programma van vandaag op en bevraagt de
  wedstrijd vanaf een kwartier voor de aftrap elke 30 seconden (instelbaar),
  tot hij is afgelopen. Er is geen sleutel nodig; een ander adres van de API
  kan met `VOETBALNEDERLAND_URL` of onder Connectie.
  - Begin, rust, tweede helft en einde komen **zonder minuut**; doelpunten
    krijgen de minuut en de doelpuntmaker. De afsluiting staat in de echte
    volgorde, bv. `Hattem - VSCO 61 0-1. #hatvsc`. Met "Clubnaam in berichten"
    toon je de eigen club bijvoorbeeld als `VSCO'61`.
  - Berichten kunnen later komen dan op het veld; een doelpunt zonder ingevulde
    speler komt zonder naam, en een rust die niet wordt ingevoerd geeft geen
    rustbericht.

**Een bron toevoegen** (voor ontwikkelaars): maak in `sources/` een subklasse
van `Source` met `id`, `label`, `description`, `run`, `led` en `status_rows`,
en zet hem in `build_sources` in `sources/__init__.py`. Hij verschijnt dan
vanzelf onder Connectie > Bron en club. Eigen instellingen krijgen een blok in `web.py`.

## Pushmeldingen

Bij elk wedstrijdbericht kan je telefoon een melding krijgen, ook als de app
dicht is. Tik je erop, dan opent een **deelscherm** met alleen dat bericht (en
de foto): tik op Delen. Zit je al op de startpagina, dan krijg je geen melding,
want daar zie je het bericht al.

- De melding toont het **logo**; in de statusbalk van Android staat het scorebord
  uit het logo.
- Aanzetten per apparaat via het menu, **Pushmeldingen** (met een knop
  voor een testmelding). Werkt alleen via **HTTPS**; op een iPhone alleen als de
  app op je beginscherm staat (iOS 16.4 of nieuwer).
- De melding gaat versleuteld via de pushdienst van je telefoon (Apple, Google,
  Mozilla of Microsoft). De sleutels en aanmeldingen staan in het datavolume
  (`vapid_private.pem`, `push_subscriptions.json`); afgemelde apparaten worden
  automatisch opgeruimd. Gaat de sleutel verloren, zet meldingen dan opnieuw aan.
- Pushdiensten vragen een contactadres (`VAPID_SUBJECT` in `.env`,
  standaard `https://github.com`); zet daar eventueel `mailto:jouw@adres` neer.
  Alleen de pushdiensten zien het.

## Installeren als app

De webomgeving is een installeerbare webapp, met een eigen icoon en zonder
adresbalk. Dat werkt alleen via **HTTPS** (net als delen met foto).

- **iPhone/iPad (Safari):** open de pagina, tik op Deel en kies "Zet op beginscherm".
- **Android (Chrome):** open de pagina, kies in het menu "App installeren" of
  "Toevoegen aan startscherm".
- **Pc (Chrome/Edge):** gebruik het installatie-icoon in de adresbalk.

De app bewaart geen pagina's op je apparaat; zonder netwerk zie je een melding.

Het logo staat ook groot in reliëf op de achtergrond (`static/watermark-light.svg`
en `-dark.svg`). Het logo (`static/logo.svg`, plus de PNG-iconen voor de app en de
meldingen) combineert de iconen
"scoreboard-outline" en "share-all-outline" uit
[Material Design Icons](https://pictogrammers.com/library/mdi/) van
Pictogrammers (licentie Apache 2.0).

## Berichten en sjablonen

Standaardteksten (`{minuut}` is de speelminuut, 0:05 is minuut 1; de thuisploeg
op het scorebord is de eigen club):

```
'1 De wedstrijd is begonnen! VSCO'61 - VSW 1 0-0. #vscvsw
'12 DOELPUNT VSCO'61! Naam Speler! VSCO'61 - VSW 1 1-0. #vscvsw
'30 Doelpunt VSW 1. VSCO'61 - VSW 1 1-1. #vscvsw
'45 Rust. VSCO'61 - VSW 1 1-1. #vscvsw
'46 De tweede helft is begonnen! VSCO'61 - VSW 1 1-1. #vscvsw
'90+2 AFGELOPEN! VSCO'61 - VSW 1 2-1. #vscvsw
```

Aanduidingen: `{minuut}`, `{thuis}`, `{uit}`, `{tegenstander}`, `{stand}`,
`{thuis_score}`, `{uit_score}`, `{hashtag}`, `{afsluiting}`, plus `{speler}` bij
een eigen doelpunt en `{blessuretijd}` bij het einde. Zie de pagina Berichten
voor de uitleg.

### De hashtag

`{hashtag}` is standaard de eerste drie letters van de **thuisploeg** plus de
eerste drie letters van de **uitploeg**, zonder cijfers en leestekens:
`VSCO'61 - VSW 1` wordt `#vscvsw`, `Limmen - WSV 30` wordt `#limwsv`. Bij een
uitwedstrijd staat de thuisploeg voorop: `Hattem - VSCO 61` wordt `#hatvsc`. Dit
geldt voor elke bron.

Voor één wedstrijd kun je een eigen hashtag instellen op de pagina Sjablonen, blok
**Hashtag** (met of zonder `#`; letters, cijfers en een underscore, maximaal 30
tekens). Hij geldt direct, voor de wedstrijd die nu bezig is of anders voor de
eerstvolgende, en vervalt zodra die wedstrijd is afgelopen: daarna geldt weer de
standaard. Met **Standaard gebruiken** wis je hem eerder.

- Blessuretijd telt per helft: na 45:00 in de eerste helft is het `'45+1`,
  na 90:00 in de tweede helft `'90+1` (op basis van `REGULAR_MINUTES`).
- Een eigen doelpunt wacht tot 20 seconden (instelbaar op de pagina Verbinding) op de doelpuntmaker, die in een later
  bericht komt (sjabloon `goal_home`). Komt die niet, dan volgt het sjabloon
  zonder doelpuntmaker. Een doelpunt van de tegenstander wordt direct gemeld.
- `AFGELOPEN!` in hoofdletters bij winst van de thuisploeg, anders `Afgelopen!`.
  Blessuretijd is het aantal minuten boven `REGULAR_MINUTES`.
- Een scorecorrectie (stand omlaag) geeft geen melding.
- De eerste ontvangen status is de beginsituatie en geeft geen melding.

## Foutafhandeling en reconnect

- Bij een verbroken of mislukte verbinding probeert de app opnieuw met een
  oplopende wachttijd (1 s, 2 s, 4 s ... maximaal 60 s, met een beetje
  willekeur). Na 30 seconden stabiel verbonden begint dat weer bij 1 s.
- Een geweigerd token (HTTP 401 of 403) wordt als zodanig gemeld en wordt maar
  eens per minuut opnieuw geprobeerd. Het opslaan van een nieuw token of een
  nieuwe URL slaat de wachttijd over.
- Onleesbare of onverwachte berichten worden gelogd en overgeslagen; de
  verbinding blijft open.
- Bij een reconnect blijft de wedstrijdstatus bewaard, zodat tijdens de storing
  gemiste wijzigingen alsnog worden gemeld. Bij een andere URL begint de
  beginsituatie opnieuw.
- Een wachtend doelpunt wordt gemeld voordat de verbinding wordt hersteld of de
  app stopt.
- Het token wordt uit alle logregels en foutmeldingen gehaald.

## Logging

Berichten en fouten staan in de console en in `logs/app.log`. Ruwe
websocketberichten staan alleen in `logs/websocket-raw.log` (roterend, 5 MB,
3 reserveposten; pad aanpasbaar met `RAW_LOG_FILE`) en dus nooit in de console.
Dat bestand kan spelersnamen bevatten. De map `logs/` staat in `.gitignore`.

## Tests

```powershell
python -m unittest
```
