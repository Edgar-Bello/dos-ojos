# Dos Ojos by text message

The front door for farmers. A farmer texts a number; the bot asks, one question
at a time and in Spanish or English, everything the water checkbook and the
drone need. It reads every date and amount back with the weekday, and stores it
only after the farmer says yes. The answers go straight into the files the
satellite and drone halves already read, and the answers come back the same way:
text AGUA and each field's days of water left arrive by text.

Nothing is sent to a real phone until Twilio keys are set. Until then every text
stays on this computer, in the phone simulator and the terminal chat.

## What the bot asks, and in what order

For each field, skipping whatever is already known:

1. What the farmer calls it, and about how many acres it is.
2. Where it is: a Google Maps pin (a short link is followed), coordinates, or
   the place in words for the team to find.
3. A link to a map page. The farmer taps the field's corners on aerial imagery,
   sees the acres, and saves. The acres are checked against what they said.
4. The crop (a numbered menu), then the planting date. For cane it asks for the
   planting or the last cut; citrus has none.
5. How it is watered (furrows, flood or borders, drip, sprinklers, pivot, or
   not irrigated), and for surface water, which side it comes in from.
6. The last irrigation, with inches if known, then any earlier ones since
   planting. A photo of the district water ticket is taken as proof.

After that a farmer texts when something happens:

| Text | Means |
|---|---|
| `AGUA` / `WATER` | How each field is doing: days left, when to water, how much |
| `REGUE hoy 4` / `WATERED 9/12 4` | An irrigation (the day and inches are optional) |
| `LLUVIA 1.2` / `RAIN 1.2` | A rain gauge reading, for all fields unless one is named |
| `COSECHA` / `HARVESTED` | A harvest. The bot then suggests a flight over bare soil |
| `SEMBRE sorgo` / `PLANTED` | A new crop and its planting date |
| `DRON` / `DRONE` | A link to upload a flight's photos |
| a photo | A water ticket (read as an irrigation) or a problem for the team |
| `MAPA`, `CAMPOS`, `NUEVO` | The map link, the list of fields, another field |
| `BORRAR` / `UNDO` | Take back the last entry (it is voided, never deleted) |
| `AYUDA` / `HELP`, `ALTO` / `STOP` | The list above; stop all texts |

Anything the bot does not understand is passed to the team (`todo`), and the
farmer is told so.

## Why the numbers can be trusted

- **Read back first.** "Anoté: riego en Campo Norte el mar 18 ago, 4
  pulgadas. ¿Correcto?" Nothing is stored until the yes. A correction
  ("no, fue el 17") keeps the inches and changes only the day.
- **The weekday is always shown**, which makes a wrong date obvious.
- **Two readings are asked, never guessed.** 3/4 is 4 March the US way and
  3 April the Mexican way. When both fit, the bot asks "1) mié 4 mar
  2) vie 3 abr". Each question also has a window: a "last irrigation" of 9/2 in
  September cannot mean 9 February.
- **Implausible amounts are asked again** ("¿40 pulgadas? Es mucha agua").
  Acre-feet from a ticket are converted with the field's measured acres, and the
  conversion is read back.
- **Every event keeps the text it came from** (`field_log.csv` notes say
  `sms message 123`), and the ticket photo if there was one. A correction voids
  the old entry rather than erasing it.

## The team's commands

From any folder in PowerShell, `C:\Users\edgar\Projects\Dos_Ojos\dosojos_sms\sms.cmd <command>`:

| Command | What it does |
|---|---|
| `serve --sim [--open]` | The web side: Twilio's webhook, the map and upload pages, and the phone simulator at http://localhost:8080/sim (`--open` opens it in the browser) |
| `chat` | Text the bot from the terminal as a pretend farmer |
| `fields` | Every field, what is known and what is missing |
| `farmers`, `messages` | Who has texted in; the conversation log |
| `todo` | Texts to answer, fields to find and draw, uploaded flights to process, bare fields to fly |
| `say <phone> "..."` | Text a farmer from the team |
| `link F003` | A map link for the team to draw a field; saving it texts the farmer the acres |
| `outline F003 field.kml` | Set an outline from Google Earth (KML) or GeoJSON |
| `export` | Write `fields.geojson` and `field_log.csv` into the satellite workspace |
| `daily [--send]` | Export, fetch imagery, weather and soil, run the checkbook, then alerts |
| `remind [--send]` | Water alerts, "have you watered?" check-ins, reminders of missing facts |

`daily` and `remind` only list what they would send unless given `--send`.
`outline F001 fields.geojson --id <id>` takes one field out of a file of several.
Global options: `--data <folder>` (default `Dos_Ojos\farm_data`), `--as-of
YYYY-MM-DD` to answer as of a pinned day. A demo folder can pin its own day with
`DOSOJOS_AS_OF=YYYY-MM-DD` in its `sms\sms.env`, so every command on it agrees.

## Where things go

```
Dos_Ojos/farm_data/            real farmers' data: never in a repository
  sms/sms.sqlite               farmers, fields, events, every message
  sms/media/                   photos sent by text
  sms/sms.env                  Twilio keys, written by you (see sms.env.example)
  dosojos_sat/                 fields.geojson, field_log.csv, cache/, out/
  dosojos_drone/               flights.json, data/raw/<field>-<date>/ uploads
```

The drone half reads `../dosojos_sat/fields.geojson` from its workspace, so both
halves run on `farm_data` unchanged: `--workspace farm_data\dosojos_sat` and
`--workspace farm_data\dosojos_drone`. Photogrammetry stays a team step. An
upload lands in `data/raw/<flight>` and is registered in `flights.json`, and
`todo` lists it until the drone half has processed it.

## Alerts, consent and quiet hours

- The bot asks before sending any alert. A farmer who says no only gets answers
  to their own texts.
- At most one unasked text per farmer per run:
  - a "water within 3 days" or "water now" alert, once per irrigation cycle;
  - three days after "water now" with no irrigation reported, "¿ya regó?";
  - otherwise the next missing fact, at most weekly.
- Nothing unasked goes out between 8 pm and 8 am farm time; it waits for the
  morning. Answers to a farmer's own action, such as a map just saved, go at
  once.
- `ALTO`, `PARAR`, `STOP` and the other carrier stop words end all texts until
  `ALTA` or `START`. A farmer who has stopped is never texted.
- Messages avoid á, í, ó and ú, which the SMS alphabet lacks; one of them would
  make every text cost two or three. ñ, é, ¿ and ¡ are in it. A test checks that
  every message fits, in both languages, in three texts at most.

## The demo

```powershell
C:\Users\edgar\Projects\Dos_Ojos\dosojos_sms\examples\setup_sms_demo.cmd
C:\Users\edgar\Projects\Dos_Ojos\dosojos_sms\examples\run_sms_demo.cmd
```

**Real fields, made-up farmers.** The four fields are real Rio Grande Valley
fields, picked from the USDA's public 2025 crop map (`examples\demo_fields.geojson`;
see `public_demo\rgv-crops-2025\SOURCE.md`):

- cotton and corn side by side near Lyford;
- grain sorghum near Elsa;
- citrus near Monte Alto.

Three MADE-UP farmers on pretend 555 numbers register them by text. Their
planting and watering dates are invented to match what the satellite saw.

| Farmer | Texts in | Field | Watered by |
|---|---|---|---|
| Juan Ejemplo, 956-555-0123 | Spanish | F001 Algodon Lyford (cotton, planted 14 Mar) | furrows, from the west |
| | | F002 Maiz Lyford (corn, planted 21 Feb, watered "ayer") | furrows, from the west |
| Maria Ejemplo, 956-555-0142 | Spanish | F003 Sorgo Elsa (grain sorghum, planted 12 Mar) | rainfed (temporal) |
| Mary Example, 956-555-0187 | English | F004 Monte Alto Grove (citrus) | flooding, from the north |

The demo is pinned to Tuesday 20 May 2025, so its answers never drift. The pin
is `DOSOJOS_AS_OF` in `examples\demo_data\sms\sms.env`, and the map links in the
texts keep working under it.

**Set-up.** The first script runs once. It needs the internet and takes about 30
minutes, mostly satellite images:

1. It replays the three conversations (`demo_juan.txt`, `demo_maria.txt`, `demo_mary.txt`).
2. It draws the four outlines, which texts each farmer the acres and a link to
   see the field on the map.
3. It runs `daily --send`: satellite, gridMET weather, SSURGO soil, the checkbook,
   and the alerts. The alerts stay in the simulator.
4. It fetches the government lidar ground under the two furrow fields.

**What each field answers to AGUA** (Juan and Mary also get an alert at set-up):

- **Cotton:** 1 day of water left (0 to 2), at first bloom. The ground line names
  the two low corners where water stands, from the 2019 lidar.
- **Corn:** about 5 days (3 to 7), since it was watered yesterday, plus its one
  low corner.
- **Citrus:** water now, about 4.7 inches by flooding, at bloom and fruit set.
  Answer "WATERED today 5", then "yes", and it comes back with about 16 days.
- **Sorghum:** it needs rain now, at boot to flowering "when drought hurts most".
  It is rainfed, so the bot talks about rain, not watering.

**The simulator.** The second script opens it. Switch between the three phones,
text AGUA, report a watering (REGUE hoy 5 / WATERED today 4) and see the days
reset, or press "+ nuevo" to sign up another pretend farmer live, map page
included. Everything lives in `examples\demo_data`, with an EXAMPLE band on every
page, apart from real farmers.

## Going live with Twilio

These steps are yours to do. They need an account, a card and your own
details, and nothing here does them for you.

1. **An account and a number.** Create a Twilio account and buy a local Texas
   number that can send SMS. The number costs about a dollar a month, and each
   text about a cent. Check Twilio's current prices.
2. **Registration (A2P 10DLC).** US carriers block texts from unregistered
   business numbers. Register a brand, as a sole proprietor or with the
   business's EIN, and a campaign, then wait for approval (days to a few weeks).
   The campaign asks for:
   - how people opt in: they text the number first, and the bot asks before any
     alert;
   - sample messages: copy them from the simulator;
   - STOP and HELP handling, which the bot does.
3. **A public https address.** Twilio must reach `serve` on its webhook, and
   phones must open the map and upload links. Use a tunnel to this computer,
   or later a small always-on server. Then set `DOSOJOS_PUBLIC_URL` and the
   number's "A message comes in" webhook to `<that address>/sms/twilio`.
4. **Keys.** Copy `sms.env.example` to `farm_data\sms\sms.env` and fill it in.
   `serve` then says "Twilio on", and every incoming call is checked against
   Twilio's signature.

Farmers' numbers, fields and water records stay in `farm_data` on this computer.
Only the texts themselves pass through Twilio.

## Setup

It runs in the satellite half's environment, which it uses to measure outlines
and run the checkbook:

```powershell
cd C:\Users\edgar\Projects\Dos_Ojos\dosojos_sms
..\dosojos_sat\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

On this computer the environment's setuptools could not build an editable
install without the `wheel` package, and fetching it would have been a download.
So the same small import hook pip writes was placed in the environment by hand:
`__editable__.dosojos_sms-0.1.0.pth`, with a `dosojos_sms-0.1.0.dist-info`
record. `pip uninstall dosojos-sms` removes it, and the command above replaces
it once pip can fetch.

Only the standard library and what the satellite half already has: no web
framework, no Twilio library. The map page loads Leaflet from cdnjs and aerial
imagery from the USGS National Map (public domain, no key).

## Layout

```
src/
  config.py     settings, the farm_data layout, keys from sms.env
  text.py       every message in Spanish and English; the SMS alphabet
  parse.py      dates, inches, crops, methods, sides, places, yes and no
  store.py      SQLite: farmers, fields, events, messages, links, uploads
  bot.py        the conversation: questions, actions, read-backs
  status.py     the checkbook and the drone's ground report, as texts
  export.py     fields.geojson, field_log.csv, flights.json
  outbox.py     sending: STOP, quiet hours, the simulator
  twilio.py     webhook signature, TwiML, sending, photos
  web.py        the HTTP server and its pages
  pages/        map.html, upload.html, sim.html
  cli.py        click commands
examples/       the demo: a made-up farmer, the placeholder outline, launchers
tests/          485 tests, no network required
```

## Tests

```powershell
..\dosojos_sat\.venv\Scripts\python.exe -m pytest -q
```

They cover every parsing rule, whole conversations in both languages, the
read-back and its corrections, the webhook over real HTTP with Twilio's own
signature test vector, the map and upload pages, quiet hours and opt-outs, the
exported files as the satellite half reads them, and the real checkbook run
in-process on a cached satellite workspace.
