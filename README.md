# VSCO scorebord-connector

Dit project maakt verbinding met de VSCO-scorebord-websocket, herkent
wedstrijdgebeurtenissen en toont ze als berichten in de console en in een
webomgeving. Daar beheer je ook de berichtteksten, de URL, het token en bekijk
je de logging. Tokens worden niet automatisch vernieuwd.

## Instellen (.env)

Kopieer `.env.example` naar `.env` (dat bestand staat in `.gitignore`):

```dotenv
WS_URL=wss://wss.ledscores.score.tel/clubs/vsco61/
WS_TOKEN=JWT <jouw-geldige-token>
REGULAR_MINUTES=90
WEB_USER=admin
WEB_PASSWORD=<kies-een-wachtwoord>
```

- `WS_URL` en `WS_TOKEN` zijn beginwaarden. Wat je later in de webomgeving
  opslaat, wordt bewaard in `data/settings.json` (in Docker: het volume
  `scorebord-data`) en **heeft voorrang** op `.env`. Wijzig je daarna het token
  in `.env`, dan merk je daar niets van: gebruik dan de webomgeving.
- Het token wordt als `token`-queryparameter aan de URL toegevoegd en correct
  URL-gecodeerd.
- `WEB_PASSWORD` is verplicht in Docker. Lokaal geldt: zonder wachtwoord staat
  de webomgeving uit en moeten `WS_URL` en `WS_TOKEN` in `.env` staan.

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

- **Wacht tot de image klaar is.** Na elke push bouwt GitHub de image; dat duurt
  een paar minuten. Pull je te vroeg, dan krijg je nog de oude versie. Kijk bij
  het tabblad Actions van de repo of de laatste run groen is.
- **Controleren:** `docker compose ps` moet `healthy` tonen en
  `docker compose logs --tail 20` moet `Webomgeving bereikbaar` en
  `Verbonden met het scorebord` bevatten.
- **Je instellingen blijven bewaard:** `.env` staat niet in git en de URL, het
  token en de sjablonen uit de webomgeving staan in het volume `scorebord-data`.
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

Inloggen met `WEB_USER` en `WEB_PASSWORD` (HTTP Basic). Formulieren hebben een
CSRF-token, scripts zijn alleen met een nonce per pagina toegestaan en alle
uitvoer wordt ge-escaped.

- **Status:** verbindingsstatus, laatste bericht en fout, volgende poging, URL
  en of er een token is, met de vervaldatum als het een JWT met `exp` is.
- **Verbinding:** URL en token wijzigen. Het token staat verborgen in het veld
  en maak je zichtbaar met het oog-icoon. Het staat alleen op deze pagina en
  alleen na inloggen. Een wijziging bouwt de verbinding direct opnieuw op. De
  URL mag geen `token=` bevatten.
- **Berichten:** de teksten van alle berichten, met voorbeeld. Een leeg
  sjabloon betekent dat dat bericht niet wordt verstuurd. Wijzigingen werken
  direct, zonder herstart.
- **Logging:** live bijgewerkt (elke 2 seconden, zonder de pagina te herladen):
  de wedstrijdberichten die de app heeft gemaakt, de laatste 300 regels van
  `app.log` (nieuwste bovenaan) en bij het laden de laatste 10 ruwe berichten,
  ingekort. Kan het logbestand niet worden geschreven, dan toont de pagina de
  regels sinds de laatste start.
- **Plaats op X:** bij elk wedstrijdbericht op de pagina Logging staat een knop
  die X (app of website) opent met het bericht al ingevuld. Je plaatst het daar
  zelf; de app heeft geen toegang tot je X-account nodig. Berichten boven 280
  tekens worden gemarkeerd.
- **Deel met foto:** bij een eigen doelpunt met doelpuntmaker staat ook de foto
  (`goalImage`, anders `image` uit de berichten) en een knop "Deel met foto". Die
  opent het deelmenu van je telefoon met tekst en foto samen; kies daar X. Dat
  werkt alleen op een telefoon of tablet en **alleen via HTTPS**, want browsers
  staan delen anders niet toe. Op een Synology regel je HTTPS met de reverse
  proxy en een certificaat in DSM. Zonder HTTPS toont de knop hoe je de foto
  zelf opslaat. De app haalt de foto zelf op en geeft alleen foto's door die in
  een wedstrijdbericht voorkomen. Let op: het gaat om foto's van spelers; of ze
  op X mogen, bepaalt de club.

## Berichten en sjablonen

Standaardteksten (`{minuut}` is de speelminuut, 0:05 is minuut 1; de thuisploeg
op het scorebord is de eigen club):

```
'1 De wedstrijd is begonnen! VSCO'61 - VSW 1 0-0. #vscvsw
'12 DOELPUNT VSCO'61! Naam Speler! VSCO'61 - VSW 1 1-0. #vscvsw
'30 Doelpunt VSW 1. VSCO'61 - VSW 1 1-1. #vscvsw
'45 Rust. VSCO'61 - VSW 1 1-1. #vscvsw
'46 De tweede helft is begonnen! VSCO'61 - VSW 1 1-1. #vscvsw
'90 + 2 AFGELOPEN! VSCO'61 - VSW 1 2-1. #vscvsw
```

Aanduidingen: `{minuut}`, `{thuis}`, `{uit}`, `{tegenstander}`, `{stand}`,
`{thuis_score}`, `{uit_score}`, `{hashtag}`, `{afsluiting}`, plus `{speler}` bij
een eigen doelpunt en `{blessuretijd}` bij het einde. Zie de pagina Berichten
voor de uitleg.

- Een eigen doelpunt wacht tot 20 seconden op de doelpuntmaker, die in een later
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
