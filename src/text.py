"""Everything the bot says, in Spanish and English, and how it fits in a text message.

A text message is 160 characters of the GSM alphabet. One character outside it
(an á, an emoji, a curly quote) turns the whole message into UCS-2, which fits
only 70, so a two-line reply costs three texts instead of one. The GSM alphabet
has ñ, é, ü, ¿ and ¡ but not á, í, ó or ú, so ``gsm_safe`` drops those accents at
the last moment. The templates below keep correct Spanish.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import date

LANGS = ("es", "en")

# --------------------------------------------------------------------------- #
# The GSM alphabet
# --------------------------------------------------------------------------- #

_GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXTENDED = "^{}\\[~]|€\f"   # two septets each
_GSM = frozenset(_GSM_BASIC) | frozenset(_GSM_EXTENDED)
#: Curly quotes, dashes, ellipsis, no-break space, degree sign and vulgar fractions.
_REPLACE = {
    chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"',
    chr(0x2013): "-", chr(0x2014): "-", chr(0x2026): "...", chr(0x00A0): " ",
    chr(0x00B0): " deg", chr(0x00BD): "1/2", chr(0x00BC): "1/4", chr(0x00BE): "3/4",
}


def gsm_safe(text: str) -> str:
    """The same message in the GSM alphabet: accents GSM lacks dropped, quotes straightened."""
    out = []
    for char in text:
        if char in _GSM:
            out.append(char)
        elif char in _REPLACE:
            out.append(_REPLACE[char])
        else:
            base = "".join(c for c in unicodedata.normalize("NFKD", char)
                           if not unicodedata.combining(c))
            out.append(base if base and all(c in _GSM for c in base) else "?")
    return "".join(out)


def segments(text: str) -> int:
    """How many texts the carrier bills for this message."""
    if all(c in _GSM for c in text):
        septets = len(text) + sum(1 for c in text if c in _GSM_EXTENDED)
        return 1 if septets <= 160 else math.ceil(septets / 153)
    units = len(text.encode("utf-16-le")) // 2
    return 1 if units <= 70 else math.ceil(units / 67)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

#: Crop keys match the water checkbook's crop models, plus other and none.
CROPS: dict[str, tuple[str, str]] = {
    "sorghum": ("sorgo", "sorghum"),
    "cotton": ("algodón", "cotton"),
    "corn": ("maíz", "corn"),
    "sugarcane": ("caña", "sugarcane"),
    "citrus": ("cítricos", "citrus"),
    "soybean": ("soya", "soybeans"),
    "other": ("otro cultivo", "another crop"),
    "none": ("sin cultivo", "no crop"),
}
#: The crop menu, in the order the question lists it.
CROP_MENU = ("sorghum", "cotton", "corn", "sugarcane", "citrus", "soybean", "other", "none")

METHODS: dict[str, tuple[str, str]] = {
    "furrow": ("surcos", "furrows"),
    "flood": ("inundación", "flooding"),
    "border": ("melgas", "borders"),
    "basin": ("cajetes", "basins"),
    "drip": ("goteo", "drip"),
    "sprinkler": ("aspersión", "sprinklers"),
    "pivot": ("pivote", "pivot"),
    "none": ("temporal", "rainfed"),
}
METHOD_MENU = ("furrow", "flood", "drip", "sprinkler", "pivot", "none")
#: Methods where water runs across the ground, so the side it enters matters.
SURFACE = ("furrow", "flood", "border", "basin")

#: What a farmer signed up for. The satellite runs for everyone and asks nothing
#: of them; the other two only add what their own camera can see, and only if
#: they have one. Nobody is ever told they need a drone.
PLANS: dict[str, tuple[str, str]] = {
    "satellite": ("sólo satélite", "satellite only"),
    "drone": ("satélite y dron", "satellite and drone"),
    "thermal": ("satélite, dron y cámara térmica", "satellite, drone and thermal camera"),
}
PLAN_MENU = ("satellite", "drone", "thermal")
#: The plans where the farmer sends us their own photos.
FLYING_PLANS = ("drone", "thermal")

SIDES = {"N": ("norte", "north"), "S": ("sur", "south"), "E": ("este", "east"),
         "W": ("oeste", "west")}

#: The stage each crop can least afford to run dry, as the checkbook names it.
STAGES: dict[str, tuple[str, str]] = {
    "sorghum": ("embuche y floración", "boot to flowering"),
    "cotton": ("floración", "bloom"),
    "corn": ("espigamiento y jiloteo", "tasseling and silking"),
    "soybean": ("llenado de vainas", "pod fill"),
    "sugarcane": ("gran crecimiento", "grand growth"),
    "citrus": ("floración y amarre", "bloom and fruit set"),
}

#: Grain sorghum growth stages as a Valley grower says them, from the heat-unit
#: model in dosojos_sat.stages (Texas A&M AgriLife B-6137).
SORGHUM_STAGES: dict[str, tuple[str, str]] = {
    "planted": ("sembrado, sin nacer", "planted, not up yet"),
    "emergence": ("recién nacido", "just emerged"),
    "three_leaf": ("3 hojas", "three-leaf"),
    "four_leaf": ("4 hojas", "four-leaf"),
    "five_leaf": ("5 hojas", "five-leaf"),
    "panicle_initiation": ("inicio de panoja", "panicle initiation"),
    "flag_leaf": ("hoja bandera", "flag leaf"),
    "boot": ("embuche", "boot"),
    "heading": ("panojeo", "heading"),
    "flowering": ("floración", "flowering"),
    "soft_dough": ("grano masoso", "soft dough"),
    "hard_dough": ("grano duro", "hard dough"),
    "black_layer": ("madurez (capa negra)", "black layer, mature"),
}

MATURITIES: dict[str, tuple[str, str]] = {
    "short": ("ciclo corto", "short season"),
    "medium": ("ciclo mediano", "medium season"),
    "long": ("ciclo largo", "long season"),
}

#: Where the drone's terrain step places a spot, in words, with the word before it
#: ("on the west side" but "in the middle").
PLACES: dict[str, tuple[str, str]] = {
    "north-east corner": ("en la esquina noreste", "in the north-east corner"),
    "north-west corner": ("en la esquina noroeste", "in the north-west corner"),
    "south-east corner": ("en la esquina sureste", "in the south-east corner"),
    "south-west corner": ("en la esquina suroeste", "in the south-west corner"),
    "north side": ("en el lado norte", "on the north side"),
    "south side": ("en el lado sur", "on the south side"),
    "east side": ("en el lado este", "on the east side"),
    "west side": ("en el lado oeste", "on the west side"),
    "middle": ("en el centro", "in the middle"),
}

MISSING: dict[str, tuple[str, str]] = {
    "acres": ("acres", "acres"),
    "location": ("ubicación", "location"),
    "map": ("mapa", "map"),
    "crop": ("cultivo", "crop"),
    "crop_other": ("cultivo", "crop"),
    "planted": ("fecha de siembra", "planting date"),
    "method": ("tipo de riego", "how it's watered"),
    "side": ("lado del agua", "water side"),
    "last_irrigation": ("último riego", "last watering"),
    "maturity": ("ciclo del sorgo", "sorghum maturity"),
}

_DAYS = {"es": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
         "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
_MONTHS = {"es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct",
                  "nov", "dic"),
           "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                  "Nov", "Dec")}
#: Month abbreviations by language, for charts that print dates.
MONTH_NAMES = _MONTHS


def pick(pair: tuple[str, str], lang: str) -> str:
    return pair[0] if lang == "es" else pair[1]


def crop_name(key: str | None, lang: str, other: str | None = None) -> str:
    if key == "other" and other:
        return other
    return pick(CROPS.get(key or "other", CROPS["other"]), lang)


def method_name(key: str | None, lang: str) -> str:
    return pick(METHODS.get(key or "furrow", METHODS["furrow"]), lang)


def plan_name(key: str | None, lang: str) -> str:
    return pick(PLANS.get(key or "satellite", PLANS["satellite"]), lang)


def day(d: date, lang: str, today: date | None = None) -> str:
    """'lun 14 sep (hoy)' / 'Mon Sep 14 (today)': the weekday makes a wrong date obvious."""
    weekday, month = _DAYS[lang][d.weekday()], _MONTHS[lang][d.month - 1]
    text = f"{weekday} {d.day} {month}" if lang == "es" else f"{weekday} {month} {d.day}"
    if today is not None and d.year != today.year:
        text += f" {d.year}" if lang == "es" else f", {d.year}"
    if today is not None:
        word = {0: ("hoy", "today"), 1: ("ayer", "yesterday")}.get((today - d).days)
        if word:
            text += f" ({pick(word, lang)})"
    return text


def about_days(n: int, lang: str) -> str:
    """'unos 5 días' / 'about 5 days', and a plain '1 día' / '1 day'."""
    if n == 1:
        return "1 día" if lang == "es" else "1 day"
    return f"unos {n} días" if lang == "es" else f"about {n} days"


def inches(value: float) -> str:
    """4 -> '4', 3.5 -> '3.5', 0.25 -> '0.25'."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def size(n_bytes: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n_bytes >= scale:
            return f"{n_bytes / scale:.1f} {unit}"
    return f"{n_bytes} bytes"


def listing(names: list[str], lang: str) -> str:
    """'A', 'A y B', 'A, B y C'."""
    joiner = " y " if lang == "es" else " and "
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + joiner + names[-1]


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

#: The first reply to a new number, before a language is known.
WELCOME = ("Dos Ojos: le decimos cuándo regar sus campos. Responda 1 para español. "
           "Reply 2 for English. Pueden aplicar cargos / Msg&data rates may apply. "
           "ALTO/STOP para salir.")

T: dict[str, tuple[str, str]] = {
    # --- getting to know the farmer ------------------------------------------
    "name": ("¿Cómo se llama?", "What's your name?"),
    "consent": (
        "Mucho gusto, {name}. ¿Le avisamos por mensaje cuando un campo necesite agua? "
        "Responda SI o NO. Unos 2 mensajes por semana; ALTO para salir.",
        "Nice to meet you, {name}. Should we text you when a field needs water? Reply YES "
        "or NO. About 2 texts a week; STOP to opt out."),
    "consent_yes": ("Listo, le avisaremos.", "Great, we'll let you know."),
    "consent_no": ("Está bien, sin avisos. Puede preguntar cuando quiera con AGUA.",
                   "OK, no alerts. You can ask any time by texting WATER."),
    # --- a field ---------------------------------------------------------------
    "field_name_first": ("Vamos a registrar su primer campo. ¿Cómo le llama? (ej. Campo Norte)",
                         "Let's add your first field. What do you call it? (e.g. North Field)"),
    "field_name": ("¿Cómo le llama a este campo? (ej. La Loma)",
                   "What do you call this field? (e.g. South 40)"),
    "acres": ("¿Cuántos acres tiene {field}, más o menos? (ej. 40, o NO SE)",
              "About how many acres is {field}? (e.g. 40, or NOT SURE)"),
    "location": (
        "¿Dónde está {field}? Mándenos un pin: en Google Maps toque el campo y luego "
        "Compartir. O mande las coordenadas, o describa el lugar (caminos, millas).",
        "Where is {field}? Send a pin: in Google Maps tap the field, then Share. Or send "
        "the coordinates, or describe the place (roads, mile lines)."),
    "location_pin": (
        "Recibido ({lat}, {lon}). Ahora marque las esquinas del campo en este mapa: {link} "
        ". Puede hacerlo después.",
        "Got it ({lat}, {lon}). Now tap the corners of the field on this map: {link} . You "
        "can do it later."),
    "location_link_failed": (
        "No pude abrir ese enlace. En Google Maps, mantenga el dedo sobre el campo, copie "
        "los números que salen (ej. 26.15, -97.99) y mándelos.",
        "I couldn't open that link. In Google Maps, press and hold on the field, copy the "
        "numbers that appear (e.g. 26.15, -97.99) and send them."),
    "location_far": ("Esa ubicación ({lat}, {lon}) no está en Estados Unidos continental. "
                     "¿Puede mandarla otra vez?",
                     "That location ({lat}, {lon}) isn't in the continental US. Could you "
                     "send it again?"),
    "location_described": ("Gracias. Nuestro equipo lo buscará en el mapa y le avisará.",
                           "Thanks. Our team will find it on the map and let you know."),
    "location_again": ("No encontré la ubicación. Mande un pin de Google Maps, las coordenadas "
                       "(ej. 26.15, -97.99) o describa el lugar con caminos o millas.",
                       "I couldn't find a location. Send a Google Maps pin, the coordinates "
                       "(e.g. 26.15, -97.99), or describe the place with roads or mile lines."),
    "crop": ("¿Qué tiene sembrado en {field}? 1 Sorgo 2 Algodón 3 Maíz 4 Caña 5 Cítricos "
             "6 Soya 7 Otro 8 Nada ahorita",
             "What's growing in {field}? 1 Sorghum 2 Cotton 3 Corn 4 Sugarcane 5 Citrus "
             "6 Soybeans 7 Other 8 Nothing now"),
    "crop_other": ("¿Qué cultivo es?", "Which crop is it?"),
    "planted": ("¿Qué día sembró el {crop} en {field}? (ej. 3/15 o 15 marzo)",
                "What day was the {crop} planted in {field}? (e.g. 3/15 or March 15)"),
    "planted_cane": ("¿Cuándo se sembró o se cortó por última vez la caña de {field}? (ej. 1/20)",
                     "When was the cane in {field} planted or last cut? (e.g. 1/20)"),
    "method": ("¿Cómo riega {field}? 1 Surcos 2 Inundación o melgas 3 Goteo 4 Aspersión "
               "5 Pivote 6 No se riega (temporal)",
               "How is {field} watered? 1 Furrows 2 Flood or borders 3 Drip 4 Sprinklers "
               "5 Pivot 6 Not irrigated (rainfed)"),
    "side": ("¿Por qué lado entra el agua a {field}? Responda N, S, E u O (norte, sur, este, "
             "oeste), o NO SE.",
             "Which side does the water come into {field} from? Reply N, S, E or W, or "
             "NOT SURE."),
    "last_irrigation": (
        "¿Cuándo fue el último riego de {field} y cuántas pulgadas? (ej. 9/2 4). Si no sabe "
        "las pulgadas, solo la fecha. Si no ha regado desde la siembra: NADA",
        "When was {field} last watered, and how many inches? (e.g. 9/2 4). If you don't know "
        "the inches, just the date. If not since planting: NONE"),
    "more_irrigation": ("¿Hubo otro riego antes de ese, desde la siembra? Mande la fecha y "
                        "pulgadas, o NO.",
                        "Was there another watering before that, since planting? Send the "
                        "date and inches, or NO."),
    "field_done": (
        "¡Listo! {field} quedó registrado. Cuando riegue, mande REGUE y la fecha (ej. REGUE "
        "hoy 4). Para ver sus campos: AGUA. Otro campo: NUEVO. Fotos de tickets de agua: "
        "bienvenidas. Todo lo que puede pedir: AYUDA.",
        "Done! {field} is registered. When you water, text WATERED and the date (e.g. "
        "WATERED today 4). To see your fields: WATER. Another field: NEW. Photos of water "
        "tickets: welcome. Everything you can ask: HELP."),
    "field_done_map": ("Falta marcar el mapa: {link}", "The map still needs its corners: {link}"),
    "resume": ("Sigamos con {field}.", "Back to {field}."),
    # --- read-backs: what we heard, for a yes or a no -------------------------
    "readback_irrigated": ("Anoté: riego en {fields} el {day}{amount}. ¿Correcto? Responda SI "
                           "o NO.",
                           "Got it: {fields} watered {day}{amount}. Correct? Reply YES or NO."),
    "amount": (", {inches} pulgadas", ", {inches} inches"),
    "amount_full": (", sin pulgadas (riego completo)", ", no inches (a full watering)"),
    "amount_converted": (", {raw} = {inches} pulgadas en {acres} acres",
                         ", {raw} = {inches} inches on {acres} acres"),
    "amount_raw": (", {raw} (sin los acres no lo paso a pulgadas)",
                   ", {raw} (without the acres I can't turn it into inches)"),
    "readback_rain": ("Anoté: lluvia de {inches} pulgadas el {day} en {fields}. ¿Correcto? "
                      "SI o NO",
                      "Got it: {inches} inches of rain {day} on {fields}. Correct? YES or NO"),
    "readback_harvested": ("Anoté: {fields} cosechado el {day}. ¿Correcto? SI o NO",
                           "Got it: {fields} harvested {day}. Correct? YES or NO"),
    "readback_planted": ("Anoté: {crop} sembrado en {fields} el {day}. ¿Correcto? SI o NO",
                         "Got it: {crop} planted in {fields} {day}. Correct? YES or NO"),
    "readback_undo": ("¿Quito esto: {what}? SI o NO", "Remove this: {what}? YES or NO"),
    "saved": ("Anotado.", "Saved."),
    "date_pick": ("¿Qué fecha es? 1) {a} 2) {b}. Responda 1 o 2.",
                  "Which date? 1) {a} 2) {b}. Reply 1 or 2."),
    "date_future": ("El {day} todavía no llega. ¿Qué día fue?",
                    "{day} hasn't come yet. Which day was it?"),
    "date_old": ("El {day} fue hace más de un año. ¿Qué día fue?",
                 "{day} is more than a year ago. Which day was it?"),
    "date_vague": ("¿Qué día exactamente? (ej. 9/2, 2 sep, ayer)",
                   "Which day exactly? (e.g. 9/2, Sep 2, yesterday)"),
    "date_missing": ("No entendí la fecha. Escríbala así: 9/2, 2 sep o ayer.",
                     "I didn't get the date. Write it like 9/2, Sep 2 or yesterday."),
    "inches_odd": ("¿{inches} pulgadas? Es mucha agua. ¿Cuántas pulgadas fueron?",
                   "{inches} inches? That's a lot of water. How many inches was it?"),
    "inches_missing": ("¿Cuántas pulgadas llovió? (ej. 0.5)", "How many inches of rain? (e.g. 0.5)"),
    "ticket_inches": ("¿Cuántas pulgadas dice el ticket? Si dice acre-pies, mándelo así: 10 af",
                      "How many inches does the ticket show? If it's acre-feet, send it like: "
                      "10 af"),
    "hours_note": (" Anoté las {hours} horas; sin el caudal no las paso a pulgadas.",
                   " I noted the {hours} hours; without the flow I can't turn them into inches."),
    "yes_no": ("Responda SI o NO.", "Reply YES or NO."),
    "menu_number": ("Responda con el número.", "Reply with the number."),
    # --- things to do --------------------------------------------------------
    "pick_field": ("¿Cuál campo? {options}", "Which field? {options}"),
    "all_fields": ("Todos", "All"),
    "no_fields": ("Todavía no tiene campos registrados.", "You don't have any fields yet."),
    "ask_irrigation_day": ("¿Qué día regó {fields} y cuántas pulgadas? (ej. hoy 4, o 9/12)",
                           "Which day did you water {fields}, and how many inches? (e.g. "
                           "today 4, or 9/12)"),
    "ask_rain": ("¿Qué día llovió y cuántas pulgadas? (ej. ayer 1.2)",
                 "Which day did it rain, and how many inches? (e.g. yesterday 1.2)"),
    "ask_harvest_day": ("¿Qué día cosechó {fields}? (ej. hoy, 9/12)",
                        "Which day did you harvest {fields}? (e.g. today, 9/12)"),
    "ask_flight_day": ("¿Qué día voló el dron sobre {fields}? (ej. hoy, ayer, 9/12)",
                       "Which day did you fly the drone over {fields}? (e.g. today, 9/12)"),
    "ask_ticket": ("Gracias por el ticket. ¿Qué fecha y cuántas pulgadas dice? (ej. 9/2 4). "
                   "Si dice acre-pies: 9/2 10 af",
                   "Thanks for the ticket. What date and how many inches does it show? (e.g. "
                   "9/2 4). If it's acre-feet: 9/2 10 af"),
    "ask_bare": ("Cuando voló, ¿el suelo de {fields} estaba sin cultivo (pelón)? SI o NO",
                 "When you flew, was {fields} bare, with no crop? YES or NO"),
    "photo_kind": ("Recibimos su foto. ¿Qué es? 1 Ticket de agua 2 Un problema en el campo "
                   "3 Otra cosa",
                   "Got your photo. What is it? 1 Water ticket 2 A problem in the field "
                   "3 Something else"),
    "photo_saved": ("Gracias, se la pasamos al equipo.", "Thanks, we passed it to the team."),
    "upload_link": ("Suba las fotos del vuelo aquí, mejor con WiFi: {link} . Puede subirlas "
                    "por partes; el enlace dura {days} días.",
                    "Upload the flight photos here, best on WiFi: {link} . You can upload in "
                    "parts; the link lasts {days} days."),
    "bare_tip": ("Con el campo sin cultivo es el mejor momento para volar el dron y medir el "
                 "nivel del suelo. Cuando tenga las fotos, mande DRON.",
                 "With the field bare it's the best time to fly the drone and measure how "
                 "level the ground is. When you have the photos, text DRONE."),
    "upload_done": ("Recibimos {n} fotos ({size}) de {field}. Nuestro equipo las procesa y le "
                    "avisa.",
                    "We got {n} photos ({size}) of {field}. Our team will process them and let "
                    "you know."),
    # --- work done right away, in the background ------------------------------
    "reading_now": ("Estoy revisando {field} con el satélite, el clima y el suelo. Le mando la "
                    "respuesta en unos minutos.",
                    "I'm reading {field} from the satellite, the weather and the soil. I'll "
                    "text you the answer in a few minutes."),
    "reading_retry": ("Todavía no pude leer {field}: un servicio del gobierno no contestó. Lo "
                      "intento otra vez en {minutes} minutos.",
                      "I couldn't read {field} yet: a government service didn't answer. I'll "
                      "try again in {minutes} minutes."),
    "reading_gave_up": ("No pude leer {field}; se lo pasé al equipo. Mande AGUA más tarde.",
                        "I couldn't read {field}; I passed it to the team. Text WATER later."),
    "ready_menu": ("También puede mandar: PORQUE (las gráficas y las cuentas){more}, AYUDA "
                   "(todo lo demás).",
                   "You can also text: WHY (the charts and the arithmetic){more}, HELP "
                   "(everything else)."),
    "menu_sorghum": (", ETAPA (cómo va el sorgo), PULGON (contar pulgón amarillo)",
                     ", STAGE (how the sorghum is doing), APHID (count sugarcane aphid)"),
    "menu_drone": (", DRON (mandar las fotos de su vuelo)", ", DRONE (send your flight)"),
    "flight_working": ("Estoy procesando el vuelo de {field}. Le aviso cuando esté listo.",
                       "I'm processing the flight over {field}. I'll text you when it's "
                       "ready."),
    "flight_ready_rows": ("El vuelo de {field} está listo: {problem} de {total} tramos de surco "
                          "necesitan una revisada ({stressed} con estrés, {missing} con plantas "
                          "faltantes). En la foto: amarillo = estrés, rayado = faltan "
                          "plantas. Todo el detalle: PORQUE.",
                          "The flight over {field} is ready: {problem} of {total} stretches of "
                          "row need a look ({stressed} stressed, {missing} with plants "
                          "missing). In the picture: yellow = stressed, hatched = missing "
                          "plants. The whole story: WHY."),
    "flight_ready_heat": ("La cámara térmica de {field} está lista. En la foto, lo rojo es lo más "
                          "caliente. Todo el detalle: PORQUE.",
                          "The thermal scan of {field} is ready. In the picture, red is "
                          "hottest. The whole story: WHY."),
    "flight_ready": ("El vuelo de {field} está listo. Todo el detalle: PORQUE.",
                     "The flight over {field} is ready. The whole story: WHY."),
    "flight_failed": ("No pude procesar el vuelo de {field}; se lo pasé al equipo.",
                      "I couldn't process the flight over {field}; I passed it to the team."),
    "map_link": ("Marque las esquinas de {field} en este mapa: {link}",
                 "Tap the corners of {field} on this map: {link}"),
    "map_saved": ("Guardamos el mapa de {field}: {acres} acres.{said} Si no está bien, mande "
                  "MAPA.",
                  "We saved the map of {field}: {acres} acres.{said} If it's wrong, text MAP."),
    "map_said": (" Usted dijo {said}.", " You said {said}."),
    "map_by_team": ("Nuestro equipo marcó {field} en el mapa: {acres} acres. Véalo aquí: "
                    "{link} . Si no está bien, mande MAPA.",
                    "Our team drew {field} on the map: {acres} acres. See it here: {link} . "
                    "If it's wrong, text MAP."),
    "fields_header": ("Sus campos:", "Your fields:"),
    "fields_missing": (" (falta: {what})", " (missing: {what})"),
    "undo_none": ("No hay nada que quitar.", "There's nothing to remove."),
    "undone": ("Quitado.", "Removed."),
    "menu": ("Listo. Mande AYUDA para ver las opciones.", "OK. Text HELP for the options."),
    "location_idle": ("Recibí una ubicación. ¿Es un campo nuevo? Mande NUEVO. Para corregir un "
                      "mapa, mande MAPA.",
                      "I got a location. Is it a new field? Text NEW. To fix a map, text MAP."),
    "lang_set": ("Listo, en español.", "OK, English it is."),
    "checkin_no": ("Gracias. Según nuestro cálculo {field} ya necesita agua; riegue en cuanto "
                   "pueda.",
                   "Thanks. By our numbers {field} needs water now; water it as soon as you "
                   "can."),
    "help": ("Dos Ojos. Mande: AGUA (cómo van), PORQUE (archivo con gráficas), REGUE fecha "
             "pulgadas, LLUVIA pulgadas, SEMBRE, COSECHA, ETAPA (cómo va el sorgo), PULGON "
             "(anotar pulgón amarillo), DRON (fotos del dron), PLAN (qué usa), CAMPOS, NUEVO "
             "(otro campo), MAPA, BORRAR (quitar lo último), ALTO (no recibir más).{contact}",
             "Dos Ojos. Text: WATER (how they're doing), WHY (file with the charts), WATERED "
             "date inches, RAIN inches, PLANTED, HARVESTED, STAGE (how the sorghum is doing), "
             "APHID (log sugarcane aphid), DRONE (drone photos), PLAN (what you use), FIELDS, "
             "NEW (another field), MAP, UNDO (remove the last entry), STOP (no more "
             "texts).{contact}"),
    "contact": (" Dudas: {contact}", " Questions: {contact}"),
    "not_understood": ("No entendí; se lo pasé al equipo. Mande AYUDA para ver las opciones.",
                       "I didn't get that; I passed it to the team. Text HELP for the options."),
    "stopped": ("Dos Ojos: no le mandaremos más mensajes. Para volver, responda ALTA.",
                "Dos Ojos: you won't get more texts. Reply START to come back."),
    "restarted": ("¡Bienvenido de vuelta a Dos Ojos! Mande AYUDA para ver las opciones.",
                  "Welcome back to Dos Ojos! Text HELP for the options."),
    # --- the water checkbook, by text -----------------------------------------
    "status_now": ("{field} ({crop}): REGAR YA. Ponga unas {gross} pulgadas por {method}.",
                   "{field} ({crop}): WATER NOW. Put on about {gross} inches by {method}."),
    "status_now_rainfed": ("{field} ({crop}): ya le falta lluvia; el cultivo está sufriendo.",
                           "{field} ({crop}): it needs rain now; the crop is under stress."),
    "status_days": ("{field} ({crop}): tiene agua para {about} ({low} a {high}); "
                    "riegue antes del {date}, unas {gross} pulgadas por {method}.",
                    "{field} ({crop}): water for {about} ({low} to {high}); water "
                    "by {date}, about {gross} inches by {method}."),
    "status_days_rainfed": ("{field} ({crop}): tiene agua para {about} ({low} a "
                            "{high}) si no llueve.",
                            "{field} ({crop}): water for {about} ({low} to {high}) "
                            "if it doesn't rain."),
    "status_plenty": ("{field} ({crop}): tiene agua para más de {days} días.",
                      "{field} ({crop}): water for more than {days} days."),
    "status_harvested": ("{field}: cosechado; no necesita agua hasta la próxima siembra.",
                         "{field}: harvested; no water needed until the next crop."),
    "status_no_data": ("{field}: todavía no tenemos los datos del satélite; le avisamos pronto.",
                       "{field}: we don't have the satellite data yet; we'll let you know soon."),
    "status_no_map": ("{field}: falta marcar el mapa: {link}",
                      "{field}: the map still needs its corners: {link}"),
    "status_no_crop": ("{field}: sin cultivo. Cuando siembre, mande SEMBRE.",
                       "{field}: no crop. When you plant, text PLANTED."),
    "status_stage": (" Está en {stage}: no lo deje secar.", " It's at {stage}: don't let it dry out."),
    "status_stage_soon": (" Pronto entra en {stage}: no lo deje secar.",
                          " It's nearly at {stage}: don't let it dry out."),
    "status_stage_rainfed": (" Está en {stage}, cuando más le duele la sequía.",
                             " It's at {stage}, when drought hurts most."),
    "status_stage_soon_rainfed": (" Pronto entra en {stage}, cuando más le duele la sequía.",
                                  " It's nearly at {stage}, when drought hurts most."),
    "status_rough": (" (Cálculo aproximado.)", " (Rough estimate.)"),
    "after_irrigation": ("{field} ahora tiene agua para {about}.",
                         "{field} now has water for {about}."),
    # --- the drone's ground report --------------------------------------------
    "ground_label": ("Terreno: ", "Ground: "),
    "ground_lidar": (" (Suelo medido con láser del gobierno en {year}.)",
                     " (Ground measured by government lidar in {year}.)"),
    "ground_high_spot": ("Hay una parte alta {where} donde el agua no llega; "
                         "rebájela al nivelar, o dele más tiempo de riego mientras.",
                         "A high spot {where} that the water doesn't reach; cut it "
                         "down when leveling, or give it a longer set until then."),
    "ground_low_spots": ("Hay partes bajas {where} donde se encharca; rellénelas al "
                         "nivelar o abra un drenaje.",
                         "Low spots {where} where water stands; fill them when leveling, "
                         "or open a drain."),
    "ground_low_spot": ("Hay una parte baja {where} donde se encharca; rellénela "
                        "al nivelar o abra un drenaje.",
                        "A low spot {where} where water stands; fill it when "
                        "leveling, or open a drain."),
    "ground_tail": ("El agua no llega al final de los surcos. Haga tiradas más cortas "
                    "(en 2 partes), más agua al inicio y luego menos, o válvulas de pulsos.",
                    "The water isn't reaching the row ends. Use shorter runs (two "
                    "sets), a bigger stream first then cut back, or surge valves."),
    "ground_far": ("El agua no cubre el fondo; melgas más chicas o más agua a la "
                   "entrada.",
                   "The water isn't covering the far end; smaller checks or more water "
                   "at the inlet."),
    "ground_head": ("La cabecera se queda mojada mucho tiempo; baje el chorro cuando "
                    "el agua llegue al final.",
                    "The inlet end stays wet too long; cut the stream back once the "
                    "water reaches the end."),
    "ground_soak_slow": (" En este barro, deje más tiempo al final.",
                         " On this clay, give the far end longer to soak."),
    "ground_soak_fast": (" En suelo arenoso, las tiradas cortas son lo principal.",
                         " On sandy soil, shorter runs matter most."),
    "ground_uneven": ("Disparejo; una nivelación con láser emparejaría el agua (unas "
                      "{yd} yardas cúbicas por acre).",
                      "Uneven; laser leveling would even out the water (about {yd} "
                      "cubic yards per acre)."),
    "ground_steep": ("Mucha pendiente para riego rodado; mejor goteo o aspersión, o "
                     "surcos en contorno.",
                     "Too steep for surface irrigation; drip or sprinklers, or rows on "
                     "the contour."),
    "ground_basin": ("Mucho desnivel para cuadros; nivélelo o use surcos.",
                     "Too much fall for level basins; level it or run furrows."),
    "ground_high": ("Las plantas débiles están en lo alto; el agua no sube ahí. "
                    "Nivelar es la solución; riegos más largos, mientras.",
                    "The weak plants sit on high ground the water doesn't climb. "
                    "Leveling is the fix; longer sets until then."),
    "ground_low": ("Las plantas débiles están en lo bajo, donde se encharca; riegos "
                   "más cortos o mejor drenaje.",
                   "The weak plants sit in low ground where water stands; shorter sets "
                   "or better drainage."),
    "ground_not_it": ("Las plantas débiles no siguen el terreno ni el final de los "
                      "surcos; revise plagas, enfermedades, nutrientes o sal.",
                      "The weak plants follow neither the ground nor the row ends; "
                      "check pests, disease, nutrients or salt."),
    # --- what they signed up for -----------------------------------------------
    "plan": (
        "¿Qué quiere usar? 1 Sólo satélite (no necesita nada) 2 Satélite y su dron "
        "3 Satélite, dron y cámara térmica (plagas). Siempre puede cambiar con PLAN.",
        "What would you like to use? 1 Satellite only (you need nothing) 2 Satellite and "
        "your drone 3 Satellite, drone and thermal camera (pests). Change any time with PLAN."),
    "plan_satellite": (
        "Listo: {plan}. No necesita dron ni nada más; el satélite pasa cada 5 días.",
        "Done: {plan}. You need no drone and nothing else; the satellite passes every 5 days."),
    "plan_drone": (
        "Listo: {plan}. El satélite sigue igual. Cuando tenga fotos de un vuelo, mande DRON "
        "y le paso el enlace para subirlas.",
        "Done: {plan}. The satellite keeps working the same. When you have photos from a "
        "flight, text DRONE and I'll send you the upload link."),
    "plan_thermal": (
        "Listo: {plan}. Suba las fotos normales y las térmicas juntas con DRON; con las "
        "térmicas le digo qué tan probable es que haya plaga y en qué parte.",
        "Done: {plan}. Upload the normal and the thermal photos together with DRONE; with "
        "thermal I can tell you how likely a pest is, and in which part."),
    "plan_license": (
        "Nota: para volar un dron sobre su rancho la ley pide licencia FAA Parte 107 "
        "(examen de $175). Usted decide si la saca, si contrata a alguien o si se queda "
        "sólo con el satélite.",
        "Note: to fly a drone over your farm the law asks for an FAA Part 107 licence "
        "($175 exam). Up to you whether to get it, hire someone, or stay on satellite only."),
    "plan_now": ("Ahora tiene: {plan}.", "Right now you have: {plan}."),
    "drone_not_in_plan": (
        "Usted está en sólo satélite. Si ya tiene fotos de un dron, mande PLAN y escoja 2 "
        "para subirlas.",
        "You're on satellite only. If you already have drone photos, text PLAN and pick 2 "
        "to upload them."),
    # --- the explanation file ----------------------------------------------------
    "explain_offer": ("¿Quiere saber por qué? Responda PORQUE y le mando un archivo con las "
                      "gráficas.",
                      "Want to know why? Reply WHY and I'll send you a file with the charts."),
    "explain_link": ("Por qué {field}: {link} . Trae las gráficas del satélite, la cuenta del "
                     "agua y el terreno. El enlace dura {days} días.",
                     "Why {field}: {link} . It has the satellite charts, the water arithmetic "
                     "and the ground. The link lasts {days} days."),
    "explain_none": ("Todavía no puedo explicar nada: falta que el satélite mire sus campos. "
                     "Mande AGUA en unos días.",
                     "I can't explain anything yet: the satellite hasn't looked at your fields. "
                     "Text WATER in a few days."),
    # --- thermal: a possible pest ---------------------------------------------------
    "pest_chance": ("Cámara térmica: {chance}% de probabilidad de plaga o enfermedad {where} "
                    "de {field} ({area} m2). Vaya a verlo; la cámara no sabe qué es.",
                    "Thermal camera: {chance}% chance of a pest or disease {where} of {field} "
                    "({area} m2). Go and look; the camera can't name it."),
    "pest_more": (" Hay {n} manchas más; vienen en el archivo de PORQUE.",
                  " There are {n} more patches; they're in the WHY file."),
    # --- sorghum -------------------------------------------------------------------
    "maturity": ("¿El sorgo de {field} es de ciclo corto, mediano o largo? Lo dice la bolsa de "
                 "la semilla. 1 Corto (temprano) 2 Mediano 3 Largo (tardío) 4 No sé",
                 "Is the sorghum in {field} a short, medium or long season hybrid? The seed "
                 "bag says. 1 Short (early) 2 Medium 3 Long (late) 4 Not sure"),
    "maturity_saved": ("Anotado: {field} es de {maturity}.", "Saved: {field} is {maturity}."),
    "maturity_unknown": ("Está bien: lo calculo como ciclo mediano. Si lo averigua, mande CICLO.",
                         "That's fine: I'll work it out as medium season. If you find out, "
                         "text MATURITY."),
    "sorghum_none": ("Esto es para sorgo, y no tiene campos de sorgo registrados. Para agregar "
                     "uno: NUEVO.",
                     "This is for sorghum, and you have no sorghum fields. To add one: NEW."),
    "pick_sorghum": ("¿Cuál campo de sorgo? {options}", "Which sorghum field? {options}"),
    "stage_report": ("{field}: va en {stage}, día {day} desde la siembra.{rest}",
                     "{field}: at {stage}, day {day} after planting.{rest}"),
    "stage_next": (" Sigue {stage} hacia el {date}.", " Next: {stage} around {date}."),
    "stage_critical": (" Esta etapa decide la cosecha.", " This stretch sets the yield."),
    "stage_assumed": (" (Lo calculé como ciclo mediano; mande CICLO para cambiarlo.)",
                      " (Worked out as medium season; text MATURITY to change it.)"),
    "watch_midge": (" Revise mosquita cada 3 días, de 10 a 2: el umbral es 1 por panoja.",
                    " Check for midge every 3 days, 10 to 2: the threshold is 1 per head."),
    "watch_aphid": (" Revise pulgón amarillo cada semana y mande PULGON.",
                    " Scout for sugarcane aphid weekly and text APHID."),
    "watch_headworm": (" Vea también gusano de la panoja.", " Look for headworms too."),
    "watch_harvest": (" Se acerca la cosecha.", " Harvest is coming."),
    "stage_no_planting": ("{field}: para calcular la etapa necesito la fecha de siembra. Mande "
                          "SEMBRE y la fecha.",
                          "{field}: to work out the stage I need the planting date. Text "
                          "PLANTED and the date."),
    "stage_no_weather": ("{field}: todavía no tengo las temperaturas para calcular la etapa; "
                         "le aviso pronto.",
                         "{field}: I don't have the temperatures to work out the stage yet; "
                         "I'll let you know soon."),
    "status_sorghum_stage": (" Va en {stage} (día {day}).", " At {stage} (day {day})."),
    "aphid_howto": ("Pulgón amarillo en {field} ({stage}): revise 4 partes del campo, 20 "
                    "plantas en cada una, una hoja de abajo y una de arriba. El umbral ahora es "
                    "{threshold}% de plantas con colonias y mielecilla. Mande cuántas tenían, "
                    "ej. 12 de 80.",
                    "Sugarcane aphid in {field} ({stage}): check 4 spots in the field, 20 plants "
                    "in each, one lower and one upper leaf. The threshold now is {threshold}% of "
                    "plants with colonies and honeydew. Send how many had them, e.g. 12 of 80."),
    "aphid_howto_mature": ("{field} ya está maduro: el pulgón amarillo sólo se trata si la "
                           "mielecilla va a atascar la cosechadora. Si quiere anotarlo, mande "
                           "cuántas plantas tenían, ej. 12 de 80.",
                           "{field} is mature: sugarcane aphid is only treated if honeydew would "
                           "gum up the combine. To log it anyway, send how many plants had them, "
                           "e.g. 12 of 80."),
    "aphid_howto_nostage": ("Pulgón amarillo en {field}: revise 4 partes del campo, 20 plantas "
                            "en cada una, una hoja de abajo y una de arriba. Mande cuántas "
                            "tenían, ej. 12 de 80.",
                            "Sugarcane aphid in {field}: check 4 spots in the field, 20 plants "
                            "in each, one lower and one upper leaf. Send how many had them, "
                            "e.g. 12 of 80."),
    "aphid_above": ("{field}: {pct}% de plantas con pulgón. En {stage} el umbral es "
                    "{threshold}%, y ya lo pasó. Hable hoy con su técnico o con AgriLife sobre "
                    "aplicar: cada día cuenta.",
                    "{field}: {pct}% of plants with aphids. At {stage} the threshold is "
                    "{threshold}%, and it's past it. Talk to your crop advisor or AgriLife "
                    "today about spraying: every day counts."),
    "aphid_near": ("{field}: {pct}% de plantas con pulgón, cerca del umbral de {threshold}% en "
                   "{stage}. Revise otra vez en 3 o 4 días.",
                   "{field}: {pct}% of plants with aphids, close to the {threshold}% threshold "
                   "at {stage}. Check again in 3 or 4 days."),
    "aphid_below": ("{field}: {pct}% de plantas con pulgón, abajo del umbral de {threshold}% en "
                    "{stage}. Revise cada semana; si ya hay pulgón, dos veces por semana.",
                    "{field}: {pct}% of plants with aphids, below the {threshold}% threshold at "
                    "{stage}. Check weekly; twice a week once aphids are there."),
    "aphid_none": ("{field}: sin pulgón esta vez. Revise otra vez en una semana.",
                   "{field}: no aphids this time. Check again in a week."),
    "aphid_harvest": ("{field}: {pct}% con pulgón, pero ya está maduro: sólo se trata si la "
                      "mielecilla va a atascar la cosechadora. Pregunte a su técnico.",
                      "{field}: {pct}% with aphids, but it's mature: only treat if honeydew "
                      "would gum up the combine. Ask your crop advisor."),
    "aphid_saved_nostage": ("{field}: anoté {pct}% de plantas con pulgón. Para decirle si pasó "
                            "el umbral necesito la fecha de siembra: mande SEMBRE y la fecha.",
                            "{field}: saved {pct}% of plants with aphids. To tell you if it's "
                            "past the threshold I need the planting date: text PLANTED and the "
                            "date."),
    "aphid_count_again": ("No entendí la cuenta. Mande cuántas plantas tenían pulgón de cuántas "
                          "revisó (ej. 12 de 80) o el porcentaje (ej. 15%). Si no encontró: 0.",
                          "I didn't get the count. Send how many plants had aphids out of how "
                          "many you checked (e.g. 12 of 80) or the percent (e.g. 15%). If none: "
                          "0."),
    # --- texts nobody asked for -------------------------------------------------
    "alert_now": ("Aviso Dos Ojos: {field} necesita agua ya. Ponga unas {gross} pulgadas por "
                  "{method}. Si ya regó, mande REGUE y la fecha.",
                  "Dos Ojos alert: {field} needs water now. Put on about {gross} inches by "
                  "{method}. If you already watered, text WATERED and the date."),
    "alert_soon": ("Aviso Dos Ojos: {field} necesitará agua en {about}, antes del "
                   "{date}. Si ya regó, mande REGUE y la fecha.",
                   "Dos Ojos alert: {field} will need water in {about}, by {date}. "
                   "If you already watered, text WATERED and the date."),
    "checkin": ("Dos Ojos: ¿ya regó {field}? Si sí, mande REGUE y la fecha (ej. REGUE 9/12 4). "
                "Si no, responda NO.",
                "Dos Ojos: have you watered {field}? If so, text WATERED and the date (e.g. "
                "WATERED 9/12 4). If not, reply NO."),
    "remind_missing": ("Dos Ojos: para calcular el agua de {field} nos falta un dato. ",
                       "Dos Ojos: to work out {field}'s water we need one more thing. "),
    "remind_map": ("Dos Ojos: falta marcar el mapa de {field}: {link}",
                   "Dos Ojos: {field}'s map still needs its corners: {link}"),
    "alert_scout": ("Dos Ojos: {field} va en {stage}. Esta semana revise pulgón amarillo{midge} "
                    "y mande PULGON con lo que encuentre.",
                    "Dos Ojos: {field} is at {stage}. This week scout for sugarcane aphid"
                    "{midge} and text APHID with what you find."),
    "alert_scout_midge": (" y mosquita (el umbral es 1 por panoja)",
                          " and midge (the threshold is 1 per head)"),
}


def say(key: str, lang: str, **values: object) -> str:
    """One message in the farmer's language, with its blanks filled."""
    template = pick(T[key], lang if lang in LANGS else "es")
    return template.format(**values)
