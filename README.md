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
`HOST_PORT` in `.env`) en is standaard alleen vanaf deze computer bereikbaar.
Wil je hem op je netwerk gebruiken, haal dan `127.0.0.1:` weg uit
[compose.yaml](compose.yaml) en gebruik een sterk wachtwoord; de verbinding is
niet versleuteld, dus zet er HTTPS voor als hij buiten je eigen netwerk komt.

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

- **Privé-repo:** de image is dan ook privé. Maak op GitHub een token met alleen
  `read:packages` en log eenmalig in: `echo <token> | docker login ghcr.io -u <jouwnaam> --password-stdin`.
  Voor `git clone` gebruik je hetzelfde soort token of een deploy key. Je kunt
  het pakket ook publiek maken (Packages, Package settings).
- **`.env` komt nooit in GitHub:** het staat in `.gitignore`, net als `logs/` en
  `data/`. Zet je token dus alleen op de NAS, of in de webomgeving.
- Kan de NAS `./logs` niet schrijven, dan start de app toch en logt hij alleen
  naar de console (en toont hij een waarschuwing).
- De webomgeving staat standaard op `127.0.0.1:8080` van de NAS. Wil je er vanaf
  je pc bij, haal dan `127.0.0.1:` weg uit [compose.yaml](compose.yaml), kies een
  sterk wachtwoord en zet er HTTPS voor als je hem buiten je eigen netwerk
  gebruikt.

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
- **Logging:** de laatste 300 regels van `app.log` (nieuwste bovenaan) en de
  laatste 10 ruwe berichten, ingekort.

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
