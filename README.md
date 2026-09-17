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

First, once: their name, whether they want alerts, and **which of the three
services they want**. Then, for each field, skipping whatever is already known:

1. What the farmer calls it, and about how many acres it is.
2. Where it is: a Google Maps pin (a short link is followed), coordinates, or
   the place in words for the team to find.
3. A link to a map page. The farmer taps the field's corners on aerial imagery,
   sees the acres, and saves. The acres are checked against what they said.
4. The crop (a numbered menu), then the planting date. For cane it asks for the
   planting or the last cut; citrus has none. **Grain sorghum** is also asked
   whether the hybrid is short, medium or long season ("no sé" is fine, and is
   worked out as medium), because that moves black layer by weeks.
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
| `DRON` / `DRONE` | A link to upload a flight's photos (satellite-only farmers are told how to switch) |
| `PORQUE` / `WHY` | A link to a page showing how that answer was worked out, charts and all |
| `ETAPA` / `STAGE` | Sorghum: its growth stage from heat units, the next stage and when, what to scout for |
| `PULGON 12 de 80` / `APHID 12 of 80` | Sorghum: a sugarcane aphid count, answered against this stage's threshold |
| `CICLO` / `MATURITY` | Sorghum: change the hybrid's maturity |
| `PLAN` | What they signed up for, and the menu to change it |
| a photo | A water ticket (read as an irrigation) or a problem for the team |
| `MAPA`, `CAMPOS`, `NUEVO` | The map link, the list of fields, another field |
| `BORRAR` / `UNDO` | Take back the last entry (it is voided, never deleted) |
| `AYUDA` / `HELP`, `ALTO` / `STOP` | The list above; stop all texts |

Anything the bot does not understand is passed to the team (`todo`), and the
farmer is told so.

## Sorghum

The Valley grows a lot of grain sorghum (the 2025 USDA crop map shows about 410
sorghum fields in the Hidalgo, Cameron and Willacy search box alone) and the
growers have few tools built for it. So sorghum gets more than the water
checkbook, **all of it with the satellite alone, no drone**:

**Its growth stage, from heat, not the calendar.** Modern hybrids ignore day
length; how far along a crop is depends on the heat it has had. The checkbook
adds up each day's growing degree units from gridMET's highs and lows, the way
Texas A&M AgriLife bulletin B-6137 (*Sorghum Growth and Development*) teaches:
`(high + low) / 2 - 50` in Fahrenheit, both ends held between 50 and 100 F, and
the bulletin's Table 1 gives the total to reach each stage for short- and
long-season hybrids (a medium hybrid is taken halfway). On the demo's pinned day
this puts Maria's dryland field, planted 12 March, at flowering, which is just
what AgriLife's Weslaco IPM newsletter was reporting across the Valley that week.

- The water warning follows the real stage. **Panicle initiation to flowering**
  is when each head sets its grain number, 70% of the yield (B-6137), so that
  stretch, not a fixed day 50-80, is when "don't let it dry out" is said. A cool
  February planting is weeks behind a warm April one at the same day count.
- `AGUA` names the stage. `ETAPA` answers with the stage, the day count, the next
  stage and its projected date (from the last two weeks' heat), and the two things
  most worth scouting for now.

**Sugarcane aphid, counted against the right threshold.** `PULGON` on its own
explains how to scout (4 spots, 20 plants each, a lower and an upper leaf) and
gives the threshold for today's stage; the farmer replies with a count (`12 de
80`, `15%`, `ninguno`). Thresholds follow the Sorghum Checkoff's *Sugarcane Aphid*
guide (2021): 20% of plants with colonies and honeydew before heading, 30% from
heading through hard dough, and at black layer only if honeydew would stop the
combine. The Valley's own AgriLife PestCast (Weslaco, 20 May 2023) applies the
same 30% in grain fill. Past the threshold the reply says to talk to their crop
advisor or AgriLife today, and the message is flagged for the team; near it,
look again in 3 or 4 days; below it, keep the weekly habit. **No product is ever
named**: the label and the advisor decide that.

**A weekly reminder**, one a week per sorghum field while aphid is worth scouting,
watered or not, and it adds sorghum midge (1 per head, AgriLife PestCast) while
the crop heads and flowers. It ranks below any water alert, so a farmer still gets
at most one text a day.

**The `PORQUE` page** gains a sorghum section: heat piling up since planting with
each stage's line and the critical stretch shaded, today's point and the projected
path; a table of every stage with the date reached or expected and what to check
then; the scouting guidance with its sources; and every aphid count the farmer
has sent.

## What a farmer signs up for

Asked once, straight after consent, and changed any time with `PLAN`. The point
of the menu is that **the first option needs nothing at all**: no drone, no
licence, no hardware. A farmer who never buys anything still gets the whole
satellite side, every five days, forever.

| | What it adds | What it costs them |
|---|---|---|
| **1 Satellite only** | Everything: when to water, how much, and the ground from public airborne lidar where it exists | nothing |
| **2 Satellite and drone** | Their own flight's photos: canopy height, row gaps, plant-by-plant flags, a ground map of their own | a drone and a licence |
| **3 ...and a thermal camera** | Warm patches scored for pests or disease, with the reasoning | a thermal camera too |

Picking 2 or 3 gets one extra text, and it is a warning rather than a sales
pitch: flying a drone over your own farm for your own business is commercial
use, so US law asks for an **FAA Part 107 certificate** — a $175 exam, not the
thousands people assume, but still a real hurdle. **How they get the photos is
entirely up to them**: fly it themselves with the certificate, buy a drone that
flies its own grid, or have someone who already holds one fly it. We never tell
a farmer they need a drone, and we never tell them they can skip the licence.

`DRON` is refused to a farmer on option 1, with the one message that switches
them over. The thermal answer only ever reaches option 3.

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
- **The whole working is one text away.** Every AGUA ends with an offer, not a
  file: *"¿Quiere saber por qué? Responda PORQUE."* See below.

## The "why" file (`PORQUE` / `WHY`)

A text message can say "water in about a day". It cannot show the arithmetic,
and a grower asked to open a valve on our say-so is owed the arithmetic. So AGUA
offers a link, one per field, and sends nothing unless they ask. The page holds:

- **the same sentence the text message said**, at the top, so the two can be
  checked against each other;
- **the four steps**, with that field's own numbers: what its soil holds, what
  went in from their own texts, what the sun took out (reference ET from gridMET
  times a crop factor read off the satellite), and what is left;
- **the satellite chart**: this year's greenness against the grey band of what
  *this same field* usually does on this date. The page says in plain words why
  the yardstick is the field's own history and never another farm;
- **the water chart**: the root zone as a tank, with every irrigation and rain;
- **every date the sums used**, so a wrong one is easy to spot and correct;
- **the ground**, when lidar or a flight measured it, saying who measured it and
  in which year;
- **the thermal patches**, for farmers on option 3, each with its score and the
  signs behind it;
- **where the numbers come from, and what this is not** — an account, not a
  soil-moisture probe.

The charts are the satellite half's own figures, so the team and the grower read
one picture and not two, and they are inlined as data URIs: the page is a single
file that still works saved, forwarded, or opened with no signal. `/r/<token>`
shows it and `/r/<token>/file` downloads it; the link lasts 14 days like the
others. Nothing on the page is new analysis — if the page and a text message
ever disagree, the text message is the bug.

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
| `daily [--send]` | Export, fetch imagery, weather and soil, build each field's own normal, run the checkbook, then alerts |
| `remind [--send]` | Water alerts, "have you watered?" check-ins, reminders of missing facts |

`daily` and `remind` only list what they would send unless given `--send`.

`daily` keeps five years of imagery: this season, plus the four earlier ones that
"what this field usually does on this date" is built from. Only the "why" page
draws that band, so a new farm whose first run would be slow can start with
`--years 2 --skip-baseline` and fill it in later; the water advice never needs it.
Nothing already cached is fetched twice.
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

**Real fields, made-up farmers.** The five fields are real Rio Grande Valley
fields, picked from the USDA's public 2025 crop map (`examples\demo_fields.geojson`;
see `public_demo\rgv-crops-2025\SOURCE.md`):

- cotton and corn side by side near Lyford;
- grain sorghum near Elsa, and another near Primera;
- citrus near Monte Alto.

Four MADE-UP farmers on pretend 555 numbers register them by text. Their
planting and watering dates are invented to match what the satellite saw.

They also pick a different service each, so the demo shows all three.

| Farmer | Texts in | Signed up for | Field | Watered by |
|---|---|---|---|---|
| Juan Ejemplo, 956-555-0123 | Spanish | 3 satellite, drone and thermal | F001 Algodon Lyford (cotton, planted 14 Mar) | furrows, from the west |
| | | | F002 Maiz Lyford (corn, planted 21 Feb, watered "ayer") | furrows, from the west |
| Maria Ejemplo, 956-555-0142 | Spanish | 1 satellite only | F003 Sorgo Elsa (grain sorghum, planted 12 Mar) | rainfed (temporal) |
| Mary Example, 956-555-0187 | English | 2 satellite and drone | F004 Monte Alto Grove (citrus) | flooding, from the north |
| Pedro Ejemplo, 956-555-0165 | Spanish | 1 satellite only | F005 Sorgo Primera (grain sorghum, planted 18 Feb, medium season) | furrows, from the west |

The demo is pinned to Tuesday 20 May 2025, so its answers never drift. The pin
is `DOSOJOS_AS_OF` in `examples\demo_data\sms\sms.env`, and the map links in the
texts keep working under it.

**Set-up.** The first script runs once. It needs the internet and takes about 30
minutes on an empty folder, mostly satellite images. A field whose five years of
images are not cached yet adds about 30 minutes on its own. `setup_sms_demo.cmd <folder>` builds
it somewhere else first:

1. It replays the four sign-ups (`demo_juan.txt`, `demo_maria.txt`, `demo_mary.txt`,
   `demo_pedro.txt`).
2. It draws the five outlines, which texts each farmer the acres and a link to
   see the field on the map.
3. It runs `daily --send`: satellite, gridMET weather, SSURGO soil, the checkbook,
   and the alerts. The alerts stay in the simulator.
4. It fetches the government lidar ground under the two furrow fields.
5. It runs the optional thermal step on Juan's corn. **That one mosaic is
   synthetic**: we have no thermal camera, so it is generated by
   `public_demo\thermal-synthetic\make_thermal.py`, and every figure from it
   carries a SYNTHETIC band. Delete that block from `setup_sms_demo.cmd` for a
   demo with no made-up pictures in it at all; everything else keeps working.
6. It replays a week of sorghum texts (`demo_maria_week.txt`, `demo_pedro_week.txt`)
   and leaves a clean copy of the whole demo in `examples\demo_data_clean`.

**What each field answers to AGUA** (Juan and Mary also get an alert at set-up):

- **Cotton:** 1 day of water left (0 to 2), at first bloom. The ground line names
  the two low corners where water stands, from the 2019 lidar.
- **Corn:** about 5 days (3 to 7), since it was watered yesterday, plus its one
  low corner.
- **Citrus:** water now, about 4.7 inches by flooding, at bloom and fruit set.
  Answer "WATERED today 5", then "yes", and it comes back with about 16 days.
- **Sorgo Elsa (Maria, dryland):** it needs rain now, at flowering "when drought
  hurts most", day 69 and 1,930 heat units. She doesn't know her hybrid, so it is
  worked out as medium and says so. Her week: `ETAPA` (next, soft dough about 31
  May; scout midge and sugarcane aphid) and a count of 6 of 80 plants, 8%, below
  the 30% threshold.
- **Sorgo Primera (Pedro, furrows):** water now, about 5.9 inches, at soft dough,
  day 91. His week: `AGUA` and a count of 21 of 80, 26%, close to the 30%
  threshold, so look again in 3 or 4 days. Maria also got the weekly scouting
  reminder; Pedro's water alert outranked his that day.

**Between farmers, and a farmer's own field.** `reset_sms_demo.cmd` puts the demo
back where set-up left it in seconds; what was there moves to `old_demos`, never
deleted. A farmer can sign up their own field live with "+ nuevo" and tap its
corners on the map link; `refresh_sms_demo.cmd` then fetches this season's
pictures, weather and soil for it in about 5 minutes, and AGUA, ETAPA and PULGON
answer for their field too. Its chart has no "normal" band until the full history
is fetched.

Then every AGUA ends with *"¿Quiere saber por qué? Responda PORQUE"*. Answer
`PORQUE` and each field comes back with a link to its own page of charts and
arithmetic. Juan's corn also carries a thermal line — a warm patch in one corner
with our score for it — because he is the one who signed up for a thermal camera.

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
  status.py     the checkbook, the ground report and the thermal patches, as texts
  explain.py    the "why" page: the arithmetic, the charts, the sources
  export.py     fields.geojson, field_log.csv, flights.json
  outbox.py     sending: STOP, quiet hours, the simulator
  twilio.py     webhook signature, TwiML, sending, photos
  web.py        the HTTP server and its pages
  pages/        map.html, upload.html, sim.html
  cli.py        click commands
examples/       the demo: a made-up farmer, the placeholder outline, launchers
tests/          559 tests, no network required
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
