"""The "why" file: one page showing how a field's answer was worked out.

A text message can say "water in about a day". It cannot show the arithmetic, and
a grower asked to open a valve on our say-so is owed the arithmetic. This builds
one self-contained page per field, offered after every AGUA and never sent
unasked: the charts, the numbers behind them, and what each one means in
ordinary words.

Everything here is drawn from what already exists. The satellite charts come
from the satellite half's own plotting code, the same figures the team looks at,
so a grower and the team are reading one picture and not two. Images are inlined
as data URIs so the page is a single file that keeps working after it is saved,
forwarded or opened with no signal.

Nothing on this page is new analysis. If the page and a text message ever
disagree, the text message is the bug.
"""

from __future__ import annotations

import base64
import html
import json
import logging
from datetime import date
from pathlib import Path

from dosojos_sat import charts as sat_charts
from dosojos_sat import stages as sat_stages
from dosojos_sat import water as sat_water

from . import text
from .config import Settings
from .status import FieldWater
from .store import Event, Farmer, FieldRow

log = logging.getLogger(__name__)

#: Events worth listing: what the farmer told us, which is what went into the sums.
SHOWN_KINDS = ("planted", "irrigated", "rain", "harvested")
#: How sure the AI said it was.
AI_CONFIDENCE = {"high": ("alta", "high"), "medium": ("media", "medium"), "low": ("baja", "low")}

S = {
    "title": ("Por qué: {field}", "Why: {field}"),
    "made": ("Hecho el {day} para {name}", "Made on {day} for {name}"),
    "save": ("Guardar este archivo", "Save this file"),
    "answer": ("La respuesta", "The answer"),
    "ai_title": ("Lo que recomienda la IA", "What the AI recommends"),
    "ai_first": ("Revise primero: {what}", "Check first: {what}"),
    "ai_why": ("Por qué lo dice:", "Why it says so:"),
    "ai_how": (
        "Esta recomendación la escribió un modelo de lenguaje abierto ({model}) que corre en "
        "la computadora de Dos Ojos, no en internet, después de leer todas las lecturas de este "
        "campo: el balance de agua, el satélite, la etapa, el dron y lo que usted anotó. Antes "
        "de mandarla se revisó que: {checks}. Si no pasa, le mandamos la respuesta de las "
        "cuentas del agua en su lugar. Seguridad de la IA: {confidence}.",
        "This recommendation was written by an open language model ({model}) running on the "
        "Dos Ojos computer, not on the internet, after reading every reading of this field: the "
        "water balance, the satellite, the stage, the drone and what you logged. Before it went "
        "out it was checked that: {checks}. If it fails, you get the water numbers' answer "
        "instead. The AI's confidence: {confidence}."),
    "ai_book": ("Lo que dicen las cuentas del agua solas:", "What the water numbers alone say:"),

    "found": ("Lo que encontró su vuelo", "What your flight found"),
    "model3d": ("Su campo en 3D", "Your field in 3D"),
    "trees_ai": ("Sus árboles, contados por inteligencia artificial",
                 "Your trees, counted by artificial intelligence"),
    "trees_ai_body": (
        "Un modelo entrenado encontró {trees} árboles en {rows} hileras. {look} necesitan una "
        "revisada (círculo naranja) y hay {gaps} huecos donde falta un árbol (X). Altura "
        "típica: {height} m. Vuelo del {day}.",
        "A trained model found {trees} trees in {rows} rows. {look} need a look (orange "
        "ring) and there are {gaps} gaps where a tree is missing (X). Typical height: "
        "{height} m. Flown {day}."),
    "trees_ai_how": (
        "Cómo funciona: tres redes neuronales pequeñas aprendieron de 206 árboles cítricos "
        "que el USDA midió a mano en Florida (datos públicos). Una busca el centro de cada "
        "árbol en la altura y el color de la foto; otra corrige la altura, porque el modelo 3D "
        "lee las copas bajas; otra compara cada árbol con los de su hilera.",
        "How it works: three small neural networks learned from 206 citrus trees the USDA "
        "measured by hand in Florida (public data). One finds the middle of each tree from "
        "the height and colour in the photos; one corrects the height, because the 3D model "
        "reads crowns short; one compares each tree with the others in its row."),
    "trees_ai_score": (
        "Qué tan bien le fue en hileras que nunca vio al aprender: encontró {found} de los "
        "árboles (el método anterior, {old}); la altura falla por {miss} cm típicamente "
        "(antes {old_miss} cm); de los árboles muertos, {dead} aparecieron como huecos; de "
        "los que estaban mal, atrapó {poor}. La lista naranja es para revisar, no un "
        "diagnóstico.",
        "How well it did on rows it never saw while learning: it found {found} of the trees "
        "(the old method, {old}); height misses by {miss} cm typically (before, {old_miss} "
        "cm); of the dead trees, {dead} showed up as gaps; of the trees in poor shape, it "
        "caught {poor}. The orange list is for checking, not a diagnosis."),
    "trees_ai_caution": (
        "Sólo ha visto una huerta (mandarina Bingo de 3 años). En otra huerta los números "
        "son una buena aproximación, no algo medido.",
        "It has only seen one grove (3-year-old Bingo mandarins). On another orchard the "
        "numbers are a good guess, not a measured one."),
    "trees_ai_edge": (" {n} árboles a la orilla del modelo 3D se contaron pero no se juzgaron.",
                      " {n} trees at the edge of the 3D model were counted but not judged."),
    "model3d_body": (
        "Hecho con sus {photos} fotos del dron: donde dos o más fotos vieron el mismo punto "
        "desde lugares distintos, se calcula a qué altura está. Mide {x} m por {y} m; las "
        "plantas más altas llegan a {top} m sobre el suelo que tienen debajo. Vuelo del {day}.",
        "Built from your {photos} drone photos: wherever two or more photos saw the same "
        "spot from different places, its height is worked out. It is {x} m by {y} m; the "
        "tallest plants stand {top} m over the ground under them. Flown {day}."),
    "model3d_how": ("Arrastre para girarlo, pellizque o use la rueda para acercarse.",
                    "Drag to turn it; pinch or scroll to zoom."),
    "model3d_photo": ("Colores de la foto", "Photo colours"),
    "model3d_height": ("Colores por altura", "Colour by height"),
    "model3d_stretch": ("Alturas x3", "Heights x3"),
    "model3d_reset": ("Volver", "Reset"),
    "model3d_legend": ("suelo", "ground"),
    "model3d_nogl": ("Este teléfono no puede girar el modelo; así se ve desde una esquina:",
                     "This phone can't turn the model; this is how it looks from a corner:"),
    "found_rows": (
        "{problem} de {total} tramos de surco necesitan una revisada: {stressed} con estrés "
        "y {missing} con plantas faltantes. Vuelo del {day}.",
        "{problem} of {total} stretches of row need a look: {stressed} stressed and "
        "{missing} with plants missing. Flown {day}."),
    "found_trees": (
        "{problem} de {total} árboles necesitan una revisada: {stressed} con estrés y "
        "{missing} faltantes o muertos. Vuelo del {day}.",
        "{problem} of {total} trees need a look: {stressed} stressed and {missing} missing "
        "or dead. Flown {day}."),
    "found_cells": (
        "{problem} de {total} cuadros de {cell} m del campo necesitan una revisada: "
        "{stressed} ralos o pálidos y {missing} casi sin plantas. Vuelo del {day}.",
        "{problem} of {total} squares of {cell} m need a look: {stressed} thin or pale and "
        "{missing} with almost no plants. Flown {day}."),
    "found_key": (
        "En la foto: amarillo = con estrés, vaya a verlo primero; rayado = faltan plantas; "
        "lo que no está pintado se ve sano. Cada parte se compara con el resto de SU campo "
        "ese mismo día, no con un número fijo.",
        "In the picture: yellow = stressed, go and look there first; hatched = plants "
        "missing; anything not painted looks healthy. Every part is compared with the rest "
        "of YOUR field on the same day, not with a fixed number."),
    "found_colour": (
        "Sus fotos se unieron en un solo mapa por su GPS y por lo que se ve repetido de una "
        "foto a otra. Sin alturas: esto sale solo del color, de qué tanto de cada cuadro "
        "está verde y qué tan verde. Si en su cultivo quedan calles peladas a propósito, "
        "esas también salen rayadas.",
        "Your photos were joined into one map by their GPS and by what repeats from one "
        "photo to the next. There are no heights: this comes from colour alone, how much "
        "of each square is green and how green. Where a crop leaves bare alleys on purpose, "
        "those show hatched too."),
    "how": ("Cómo salió ese número", "How that number came out"),
    "step_soil": ("1. Cuánta agua cabe en su suelo", "1. How much water your soil holds"),
    "step_soil_body": (
        "El mapa de suelos del USDA dice que {field} es {soil}. Bajo el cultivo, las raíces "
        "llegan hoy a unas {root} pulgadas de hondo, y en esa capa caben {capacity} pulgadas "
        "de agua aprovechable. El cultivo empieza a sufrir antes de vaciarla: cuando quedan "
        "menos de {stress} pulgadas.",
        "The USDA soil map says {field} is {soil}. Under the crop the roots reach about "
        "{root} inches down today, and that layer holds {capacity} inches of usable water. "
        "The crop starts to suffer before it runs out: below {stress} inches left."),
    "reason_planted": ("la siembra", "the planting"),
    "reason_irrigated": ("el primer riego que tenemos anotado", "the first watering on record"),
    "reason_assumed": ("no hay siembra ni riego anotado, así que supusimos el suelo lleno",
                       "no planting or watering on record, so we assumed the soil was full"),
    "sorghum": ("La etapa del sorgo", "Where the sorghum is"),
    "sorghum_body": (
        "{field} va en {stage}: día {day} desde la siembra, con {gdu} grados-día de calor "
        "acumulados. El sorgo avanza con el calor, no con el calendario: uno sembrado en un "
        "febrero fresco va más atrasado, a los mismos días, que uno sembrado en abril. Sumamos "
        "el calor de cada día con las temperaturas del gobierno (gridMET), como enseña Texas "
        "A&M AgriLife.",
        "{field} is at {stage}: day {day} after planting, with {gdu} heat units so far. "
        "Sorghum moves with heat, not the calendar: one planted in a cool February is behind, "
        "at the same day count, one planted in April. We add up each day's heat from "
        "government temperatures (gridMET), the way Texas A&M AgriLife teaches."),
    "sorghum_maturity": ("Lo calculamos como {maturity}, que es lo que usted nos dijo.",
                         "Worked out as {maturity}, as you told us."),
    "sorghum_maturity_assumed": (
        "No sabemos el ciclo del híbrido, así que lo calculamos como ciclo mediano. Uno de ciclo "
        "corto va unas semanas adelante y uno largo unas semanas atrás; si lo sabe, mande CICLO.",
        "We don't know the hybrid's maturity, so it's worked out as medium season. A short "
        "season hybrid runs weeks ahead and a long one weeks behind; if you know it, text "
        "MATURITY."),
    "sorghum_critical": (
        "De inicio de panoja a floración cada panoja decide cuántos granos va a llenar, y de "
        "eso sale el 70% de la cosecha. Es cuando menos le puede faltar agua o sobrarle plaga.",
        "From panicle initiation to flowering each head decides how many grains it will fill, "
        "and that is 70% of the yield. It's when it can least afford to run short of water or "
        "long on pests."),
    "sorghum_no_chart": ("Todavía no hay suficientes temperaturas para dibujar la etapa.",
                         "There aren't enough temperatures yet to draw the stage."),
    "stage_col": ("Etapa", "Stage"),
    "date_col": ("Fecha", "Date"),
    "watch_col": ("Qué revisar", "What to check"),
    "reached": ("ya llegó", "reached"),
    "expected": ("se espera", "expected"),
    "chart_stage_title": ("{field}: calor acumulado desde la siembra",
                          "{field}: heat since planting"),
    "chart_stage_sub": ("sorgo de grano, {maturity}; grados-día (F, base 50), Texas A&M B-6137",
                        "grain sorghum, {maturity}; growing degree units (F, base 50), "
                        "Texas A&M B-6137"),
    "chart_today": ("hoy", "today"),
    "chart_critical": ("aquí se decide la cosecha", "this sets the yield"),
    "chart_projected": ("si sigue el calor de estas dos semanas",
                        "if the heat stays like the last two weeks"),
    "chart_axis": ("grados-día acumulados", "heat units so far"),
    "pests": ("Plagas del sorgo: cuándo revisar", "Sorghum pests: when to check"),
    "pest_aphid": (
        "Pulgón amarillo: revise cada semana desde que nace, y dos veces por semana cuando ya "
        "encontró. Vea 4 partes del campo y 20 plantas en cada una, una hoja de abajo y una de "
        "arriba; lo primero que se nota es la mielecilla brillosa en las hojas bajas. Se trata "
        "cuando hay colonias con mielecilla en el 20% de las plantas antes del panojeo, o en el "
        "30% de panojeo a grano duro. Ya maduro, sólo si la mielecilla va a atascar la "
        "cosechadora.",
        "Sugarcane aphid: scout weekly from emergence, twice a week once you've found it. Check "
        "4 spots and 20 plants in each, one lower and one upper leaf; shiny honeydew on the "
        "lower leaves is usually the first sign. Treat when colonies with honeydew are on 20% of "
        "plants before heading, or 30% from heading to hard dough. Once mature, only if honeydew "
        "would gum up the combine."),
    "pest_midge": (
        "Mosquita del sorgo: mientras florea, revise cada 3 días entre las 10 y las 2. Busque "
        "una mosca chiquita anaranjada en las flores amarillas. El umbral es 1 por panoja.",
        "Sorghum midge: while it flowers, check every 3 days between 10 and 2. Look for a tiny "
        "orange fly on the yellow flowers. The threshold is 1 per head."),
    "pest_heads": (
        "Gusano de la panoja y chinche: vigílelos de la floración al grano duro.",
        "Headworms and stink bugs: watch for them from flowering to hard dough."),
    "pest_advice": (
        "Estos umbrales son guías generales de Texas A&M AgriLife y del Sorghum Checkoff. Un "
        "híbrido tolerante al pulgón aguanta más. Antes de aplicar, confirme con su técnico o "
        "con AgriLife en Weslaco, y lea la etiqueta.",
        "These thresholds are general guides from Texas A&M AgriLife and the Sorghum Checkoff. "
        "An aphid-tolerant hybrid holds out longer. Before spraying, check with your crop "
        "advisor or AgriLife in Weslaco, and read the label."),
    "counts": ("Sus conteos de pulgón", "Your aphid counts"),
    "counts_empty": ("Todavía no ha mandado conteos. Mande PULGON para anotar uno.",
                     "No counts yet. Text APHID to log one."),
    "verdict_above": ("pasó el umbral", "past the threshold"),
    "verdict_near": ("cerca del umbral", "close to the threshold"),
    "verdict_below": ("abajo del umbral", "below the threshold"),
    "verdict_harvest": ("maduro: sólo por la cosecha", "mature: harvest only"),
    "verdict_none": ("sin etapa", "no stage"),
    "step_in": ("2. Lo que entró", "2. What went in"),
    "step_in_body": (
        "Contamos desde {start} ({start_reason}). Desde entonces entraron {irrigation} "
        "pulgadas de riego que usted nos avisó y {rain} pulgadas de lluvia.",
        "We count from {start} ({start_reason}). Since then {irrigation} inches of "
        "irrigation you told us about went in, plus {rain} inches of rain."),
    "step_out": ("3. Lo que salió", "3. What went out"),
    "step_out_body": (
        "Cada día el sol y el aire se llevan agua. La estación del gobierno (gridMET) dice "
        "cuánta se llevaría un pasto de referencia: {eto} pulgadas al día. Su cultivo no gasta "
        "lo mismo: gasta esa cifra por un factor que sale de qué tan verde y tapado está el "
        "campo en la foto del satélite, hoy {kc}. Eso da {use} pulgadas al día.",
        "Every day the sun and the air take water away. The government weather grid (gridMET) "
        "says how much a reference grass would lose: {eto} inches a day. Your crop doesn't use "
        "the same: it uses that figure times a factor taken from how green and how covered the "
        "field looks to the satellite, today {kc}. That gives {use} inches a day."),
    "step_left": ("4. Lo que queda", "4. What's left"),
    "step_left_body": (
        "Quedan {left} pulgadas aprovechables. A {use} pulgadas al día, eso alcanza para "
        "{days}. Damos un rango ({low} a {high} días) porque el clima de los próximos días no "
        "se sabe: el rango es la cuenta con 20% más y 20% menos de gasto.",
        "There are {left} usable inches left. At {use} inches a day that lasts {days}. We give "
        "a range ({low} to {high} days) because the next few days' weather isn't known: the "
        "range is the same sum with 20% more and 20% less use."),
    "step_left_plenty": (
        "Quedan {left} pulgadas aprovechables, más de lo que el cultivo gastará en los "
        "próximos {horizon} días.",
        "There are {left} usable inches left, more than the crop will use in the next "
        "{horizon} days."),
    "chart_ndvi": ("El satélite: su campo contra sí mismo",
                   "The satellite: your field against itself"),
    "chart_ndvi_body": (
        "La línea verde es lo verde que está {field} este año, medido por el satélite "
        "Sentinel-2 cada cinco días. La banda gris es lo que ESTE MISMO campo suele tener en "
        "esta fecha, sacado de {years}. Nunca comparamos su campo con el de otro: cada suelo y "
        "cada variedad son distintos, así que el único punto de comparación justo es su propia "
        "historia. Un punto por debajo de la banda quiere decir que el campo va más atrasado "
        "que sus propios años anteriores.",
        "The green line is how green {field} is this year, measured by the Sentinel-2 satellite "
        "every five days. The grey band is what THIS SAME field usually shows on this date, "
        "from {years}. We never compare your field with anyone else's: every soil and every "
        "variety is different, so the only fair yardstick is its own history. A point below the "
        "band means the field is behind its own earlier years."),
    "chart_water": ("La cuenta del agua, día por día", "The water account, day by day"),
    "chart_water_body": (
        "Arriba, el agua que queda en la zona de raíces, como un tanque que se llena con riego "
        "y lluvia y se vacía con el sol. La línea roja punteada es donde el cultivo empieza a "
        "sufrir. La línea punteada al final es hacia dónde va si no llueve. Abajo, cada riego "
        "que usted nos avisó y cada lluvia.",
        "On top, the water left in the root zone, like a tank that fills with irrigation and "
        "rain and empties with the sun. The dashed red line is where the crop starts to suffer. "
        "The dotted line at the end is where it goes if no rain comes. Below, every irrigation "
        "you told us about and every rain."),
    "log": ("Lo que usted nos dijo", "What you told us"),
    "log_body": ("Estas son las fechas con las que se hizo la cuenta. Si alguna está mal, "
                 "mándenos el dato corregido y la cuenta cambia sola.",
                 "These are the dates the sums were made with. If one is wrong, text us the "
                 "correction and the sums change by themselves."),
    "log_empty": ("Todavía no nos ha dicho ningún riego ni lluvia de este campo, así que la "
                  "cuenta partió de un supuesto.",
                  "You haven't told us about any watering or rain on this field yet, so the "
                  "sums started from an assumption."),
    "ground": ("El terreno", "The ground"),
    "ground_lidar": (
        "El suelo bajo {field} se midió con el láser aéreo del gobierno (USGS 3DEP) en {year}. "
        "Nadie voló nada suyo. Si niveló después de esa fecha, esto no lo ve.",
        "The ground under {field} was measured by the government's airborne laser (USGS 3DEP) "
        "in {year}. Nobody flew anything of yours. If you levelled after that, this "
        "can't see it."),
    "ground_drone": ("El suelo bajo {field} salió de las fotos de su propio vuelo del {year}.",
                     "The ground under {field} came from your own flight's photos, {year}."),
    "ground_cloud": ("El suelo bajo {field} salió del láser de su propio vuelo del {year}.",
                     "The ground under {field} came from your own flight's laser, {year}."),
    "ground_why": (
        "Importa porque el agua corre cuesta abajo: una parte alta se queda seca aunque el "
        "campo entero lleve el riego completo, y una parte baja se encharca. Los colores son "
        "centímetros por encima o por debajo de un plano liso.",
        "It matters because water runs downhill: a high spot stays dry even when the whole "
        "field got its full watering, and a low spot ponds. The colours are centimetres above "
        "or below a smooth plane."),
    "canopy": ("Su vuelo: la altura del cultivo", "Your flight: how tall the crop is"),
    "canopy_body": (
        "De las fotos de su propio vuelo salen dos mapas del terreno: uno de la tierra pelona "
        "y otro de lo alto de las plantas. La resta de los dos es la altura del cultivo, "
        "planta por planta. Los colores van de bajito a alto.",
        "Your own flight's photos give two maps: one of the bare ground and one of the top of "
        "the plants. Subtracting one from the other is the crop's height, plant by plant. The "
        "colours run from short to tall."),
    "thermal": ("La cámara térmica: posibles plagas", "The thermal camera: possible pests"),
    "thermal_body": (
        "Una planta sana se enfría sola: saca agua por las hojas, como sudar. Una planta que "
        "deja de hacerlo se calienta uno o tres grados antes de que se le note nada a la vista. "
        "Eso es lo que ve esta cámara. Lo que NO puede ver es la causa: sed, agua que no llegó, "
        "o algo que se la está comiendo. Por eso cada mancha lleva un porcentaje nuestro, hecho "
        "comparándola con el terreno, con la cuenta del agua y con lo que la cámara de color ya "
        "había marcado. No es un análisis de laboratorio ni un diagnóstico: es un orden en el "
        "que vale la pena ir a caminar el campo.",
        "A healthy plant cools itself: it pulls water out through its leaves, like sweating. A "
        "plant that stops doing that runs one to three degrees hotter before anything shows to "
        "the eye. That is what this camera sees. What it can NOT see is the cause: thirst, "
        "water that never arrived, or something eating it. So every patch carries a percentage "
        "of ours, made by comparing it with the ground, with the water account and with what "
        "the colour camera had already flagged. It is not a lab test and not a diagnosis: it is "
        "an order in which to go and walk the field."),
    "thermal_patch": ("{chance}% - {where}, {area} m2, {above} C más caliente que el resto",
                      "{chance}% - {where}, {area} m2, {above} C hotter than the rest"),
    "thermal_none": ("La cámara térmica no encontró ninguna mancha caliente digna de reportar.",
                     "The thermal camera found no warm patch worth reporting."),
    "thermal_go": ("Vaya a verlo. La cámara no sabe qué es.",
                   "Go and look. The camera can't name what it is."),
    # What the camera could not do, in the farmer's own words. The drone half
    # writes the same things in English for whoever runs it; these are the ones
    # that change how much weight to put on the answer, so they go on the page.
    "thermal_by_heat": (
        "En este vuelo no venían fotos de color, así que no hay modelo de altura: las hojas se "
        "reconocieron solo por temperatura, por estar varios grados más frescas que el suelo de "
        "junto. Una planta tan dañada que ya ni se enfría se lee como suelo y se queda fuera - "
        "y esa es justo la que uno quiere encontrar. Un mapa tranquilo aquí tranquiliza menos "
        "que uno hecho con fotos de color al lado.",
        "This flight carried no colour photos, so there is no height model: leaves were told "
        "only by temperature, by being several degrees cooler than the soil beside them. A "
        "plant so far gone that it no longer cools itself reads as soil and is left out - and "
        "that is exactly the plant worth finding. A quiet map here is less reassuring than one "
        "made with colour photos beside it."),
    "thermal_scan": (
        "El vuelo tardó {minutes} minutos y el sol siguió subiendo: el cultivo se calentó "
        "{drift} C de la primera foto a la última. Eso se descuenta cuadro por cuadro, "
        "comparando cada foto con las que la traslapan. Lo que cuesta: una franja caliente más "
        "ancha que lo que ve la cámara de un jalón se va con la corrección, así que un campo "
        "caliente entero se ve en la cuenta del agua, no aquí.",
        "The flight took {minutes} minutes and the sun kept climbing: the crop warmed {drift} C "
        "between the first picture and the last. That is taken out frame by frame, by comparing "
        "each picture with the ones it overlaps. What it costs: a hot stretch wider than the "
        "camera sees at once goes with the correction, so a whole field running hot shows up in "
        "the water account, not here."),
    "thermal_all_hot": (
        "Ojo: {share}% de la mata está caliente, que es demasiado campo para ser manchas. O le "
        "falta agua parejo, o el vuelo pasó con nubes o viento. Tome lo de abajo como lugares "
        "para ir a ver, nada más.",
        "Careful: {share}% of the canopy is running hot, which is too much of the field to be "
        "patches. Either it is short of water all over, or the flight passed through cloud or "
        "wind. Treat what follows as places to go and look, nothing more."),
    "sources": ("De dónde salen los números", "Where the numbers come from"),
    "limits": ("Lo que esto no es", "What this is not"),
    "limits_body": (
        "Esto es una cuenta, no una medición de la humedad de su suelo. Se apoya en fechas que "
        "usted nos dio, en un mapa de suelos hecho por regiones y no por su campo, y en el "
        "pronóstico de que mañana se parece a hoy. Sirve para decidir a cuál campo ir primero. "
        "Si abre una calicata y ve otra cosa, la calicata tiene la razón: mándenos el dato.",
        "This is an account, not a measurement of your soil's moisture. It leans on dates you "
        "gave us, on a soil map drawn by region rather than by your field, and on the guess "
        "that tomorrow resembles today. It is for deciding which field to go to first. If you "
        "dig a hole and see otherwise, the hole is right: text us."),
    "no_season": ("(Falta la gráfica: el satélite todavía no junta suficientes fotos de este "
                  "campo.)",
                  "(Chart missing: the satellite hasn't gathered enough images of this field "
                  "yet.)"),
    "no_normal": ("(Falta la banda gris: todavía no tenemos suficientes años de este campo "
                  "para saber qué es normal en él. Se va llenando con cada temporada.)",
                  "(The grey band is missing: we don't have enough years of this field yet to "
                  "know what is normal for it. It fills in with each season.)"),
    "chart_failed": ("(La gráfica no se pudo dibujar esta vez. Los números de arriba no "
                     "dependen de ella.)",
                     "(The chart could not be drawn this time. The numbers above do not "
                     "depend on it.)"),
    "footer": ("Dos Ojos - dos ojos sobre su campo: el satélite y, si usted quiere, su dron.",
               "Dos Ojos - two eyes on your field: the satellite and, if you want, your drone."),
}

#: The thermal step's signs, in the farmer's own words. The drone half writes
#: the same list in English prose; a key it grows that is not here falls back to
#: that prose rather than going missing.
SIGNS = {
    "hot": ("corre {above_c} C arriba del resto del cultivo: uno o dos grados es normal, "
            "tres es una planta que dejó de tomar agua",
            "runs {above_c} C above the rest of the canopy: a degree or two is ordinary, "
            "three is a plant that has stopped drinking"),
    "warm": ("corre {above_c} C arriba del resto del cultivo",
             "runs {above_c} C above the rest of the canopy"),
    "faint": ("apenas está {above_c} C arriba del resto: dentro de lo que varía un campo "
              "cualquiera",
              "is only {above_c} C above the rest: within what an ordinary field varies by"),
    "big": ("cubre {area} m2, bastante para ir a verlo",
            "covers {area} m2, large enough to be worth walking out to"),
    "compact": ("es una mancha y no una raya a lo largo de los surcos: los problemas de agua "
                "siguen los surcos y la pendiente, una plaga se abre desde un punto",
                "is a blob and not a streak along the rows: water problems follow the rows and "
                "the slope, an infestation spreads outwards from a point"),
    "streak": ("va como raya a lo largo de los surcos, que es la forma de un problema de riego",
               "runs as a streak along the rows, which is the shape of a watering problem"),
    "flat_ground": ("está en terreno que el láser encontró parejo, así que no es que el agua "
                    "no le llegara",
                    "sits on ground the laser found level, so it is not that the water missed it"),
    "high_ground": ("está en una parte alta, adonde al agua le cuesta llegar: el terreno "
                    "explica el calor sin ninguna plaga",
                    "sits on a high spot the water struggles to reach: the ground explains the "
                    "heat without any pest"),
    "low_ground": ("está en una parte baja donde se encharca, y las raíces ahogadas también "
                   "dejan de tomar agua",
                   "sits in a low spot where water stands, and drowned roots also stop drinking"),
    "field_watered": ("está caliente aunque al campo todavía le queda agua: un campo con sed "
                      "se calienta parejo, no en manchas",
                      "is hot while the field still has water: a thirsty field runs hot all "
                      "over, not in patches"),
    "field_dry": ("está caliente en un campo al que ya le toca riego, así que esta mancha dice "
                  "poco",
                  "is hot on a field that is due water anyway, so this patch says little"),
    "crop_damaged": ("tiene {share} de sus plantas ya marcadas chicas o pálidas, contra {field} "
                     "en todo el campo: la cámara de color también ve el daño",
                     "holds {share} of its plants already flagged small or pale, against {field} "
                     "across the field: the colour camera sees the damage too"),
    "crop_fine": ("tiene plantas que a la cámara de color le parecen normales; caliente pero "
                  "de buen ver suele ser agua, no plaga",
                  "holds plants the colour camera finds normal; hot but healthy-looking is more "
                  "often water than pest"),
}

SOURCES = [
    ("Sentinel-2 (ESA/Copernicus)",
     ("fotos del satélite cada 5 días, gratis y públicas",
      "satellite images every 5 days, free and public")),
    ("gridMET (University of Idaho)",
     ("clima diario en cuadros de 4 km", "daily weather on a 4 km grid")),
    ("USDA SSURGO", ("mapa de suelos", "soil map")),
    ("FAO-56", ("el método de la cuenta del agua", "the water accounting method")),
]

#: Listed only when the ground on this page actually came from it. A farmer
#: whose own flight measured the ground, or who has no ground map at all, should
#: not read a list of things we used that we did not use.
GROUND_3DEP_SOURCE = ("USGS 3DEP", ("láser aéreo del gobierno para el terreno",
                                    "the government's airborne laser for the ground"))


#: What a farmer checks for at each stage, in their words.
WATCH_WORDS = {
    "sugarcane_aphid": ("pulgón amarillo", "sugarcane aphid"),
    "midge": ("mosquita", "midge"),
    "headworm": ("gusano de la panoja", "headworms"),
    "harvest": ("preparar la cosecha", "plan the harvest"),
}

SORGHUM_SOURCES = [
    ("Texas A&M AgriLife B-6137",
     ("etapas del sorgo por calor acumulado", "sorghum stages from accumulated heat")),
    ("Sorghum Checkoff, Sugarcane Aphid (2021)",
     ("umbrales de pulgón amarillo por etapa", "sugarcane aphid thresholds by stage")),
    ("AgriLife PestCast, Weslaco",
     ("mosquita y pulgón en el Valle", "midge and aphid in the Valley")),
]

STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #f7f6f2; color: #23221e;
       font: 17px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 27px; line-height: 1.2; margin: 0 0 4px; }
h2 { font-size: 21px; margin: 40px 0 10px; padding-top: 18px; border-top: 2px solid #e2e0d8; }
h3 { font-size: 17px; margin: 22px 0 4px; color: #4a483f; }
p { margin: 10px 0; }
.made { color: #6f6e69; font-size: 15px; margin: 0 0 20px; }
.banner { background: #fde68a; color: #4a3c00; padding: 10px 16px; font-weight: 600;
          text-align: center; }
.answer { background: #fff; border-left: 6px solid #199e70; border-radius: 10px;
          padding: 18px 20px; font-size: 20px; font-weight: 600; margin: 18px 0; }
.answer.now { border-left-color: #d03b3b; }
tr.now td { background: #eef3ea; font-weight: 600; }
figure { margin: 14px 0; }
img { max-width: 100%; height: auto; border-radius: 10px; background: #fff; display: block; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 16px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #e2e0d8; }
th { color: #6f6e69; font-weight: 600; }
td.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.save { display: inline-block; background: #199e70; color: #fff; text-decoration: none;
        padding: 11px 18px; border-radius: 9px; font-weight: 600; margin: 6px 0 4px; }
.patch { background: #fff; border-radius: 10px; padding: 14px 16px; margin: 12px 0;
         border-left: 6px solid #eda100; }
.patch .chance { font-size: 19px; font-weight: 700; }
.patch ul { margin: 8px 0 0; padding-left: 20px; color: #4a483f; font-size: 15px; }
.note { color: #6f6e69; font-size: 15px; }
.m3d canvas { width: 100%; height: min(72vw, 520px); display: block; border-radius: 10px;
              background: #20241f; touch-action: none; cursor: grab; }
.m3d .tools { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
.m3d button { font: inherit; font-size: 15px; padding: 8px 12px; border-radius: 8px;
              border: 1px solid #cfcdc4; background: #fff; color: #23221e; cursor: pointer; }
.m3d button[aria-pressed="true"] { background: #199e70; border-color: #199e70; color: #fff; }
.m3d .legend { display: flex; align-items: center; gap: 8px; font-size: 14px; color: #6f6e69; }
.m3d .legend i { flex: 0 0 140px; height: 10px; border-radius: 5px;
                 background: linear-gradient(90deg, #8c6b45, #edcc40, #1a8c33); }
.m3d .fallback { display: none; }
.m3d.nogl canvas, .m3d.nogl .tools, .m3d.nogl .legend { display: none; }
.m3d.nogl .fallback { display: block; }
footer { margin-top: 44px; color: #6f6e69; font-size: 14px; text-align: center; }
@media print { body { background: #fff; } .save { display: none; } }
"""

#: The 3D viewer: plain WebGL, no library, so a saved page still turns with no
#: signal. The points come inline (base64) in the layout model3d.py writes:
#: int16 x, y, z, then uint8 r, g, b, then uint8 height over the ground (0.1 m).
VIEWER_JS = r"""
(function () {
  var box = document.querySelector(".m3d");
  if (!box) return;
  var meta = JSON.parse(box.getAttribute("data-meta"));
  var canvas = box.querySelector("canvas");
  var gl = canvas.getContext("webgl", { antialias: true }) ||
           canvas.getContext("experimental-webgl");
  if (!gl) { box.className += " nogl"; return; }
  var raw = atob(box.querySelector("script.points").textContent.replace(/\s+/g, ""));
  var n = meta.points, bytes = new Uint8Array(raw.length), i;
  for (i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  var data = new DataView(bytes.buffer), pos = new Float32Array(n * 3);
  for (i = 0; i < n * 3; i++) pos[i] = data.getInt16(i * 2, true) * meta.scale_m;

  function shader(kind, source) {
    var s = gl.createShader(kind);
    gl.shaderSource(s, source);
    gl.compileShader(s);
    return s;
  }
  var program = gl.createProgram();
  gl.attachShader(program, shader(gl.VERTEX_SHADER,
    "attribute vec3 p; attribute vec3 c; attribute float h;" +
    "uniform mat4 m; uniform float stretch, ground, top, byHeight, size;" +
    "varying vec3 v;" +
    "void main() {" +
    "  gl_Position = m * vec4(p.x, p.y, ground + (p.z - ground) * stretch, 1.0);" +
    "  gl_PointSize = clamp(size / gl_Position.w, 1.0, 14.0);" +
    "  float t = clamp(h * 25.5 / top, 0.0, 1.0);" +
    "  vec3 ramp = t < 0.5 ? mix(vec3(0.55, 0.42, 0.27), vec3(0.93, 0.80, 0.25), t * 2.0)" +
    "                      : mix(vec3(0.93, 0.80, 0.25), vec3(0.10, 0.55, 0.20), t * 2.0 - 1.0);" +
    "  v = mix(c, ramp, byHeight);" +
    "}"));
  gl.attachShader(program, shader(gl.FRAGMENT_SHADER,
    "precision mediump float; varying vec3 v; void main() { gl_FragColor = vec4(v, 1.0); }"));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { box.className += " nogl"; return; }
  gl.useProgram(program);

  function attribute(name, array, size, type, normalized) {
    var buffer = gl.createBuffer(), where = gl.getAttribLocation(program, name);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, array, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(where);
    gl.vertexAttribPointer(where, size, type, normalized, 0, 0);
  }
  attribute("p", pos, 3, gl.FLOAT, false);
  attribute("c", bytes.subarray(n * 6, n * 9), 3, gl.UNSIGNED_BYTE, true);
  attribute("h", bytes.subarray(n * 9, n * 10), 1, gl.UNSIGNED_BYTE, true);
  var u = {};
  ["m", "stretch", "ground", "top", "byHeight", "size"].forEach(function (k) {
    u[k] = gl.getUniformLocation(program, k);
  });

  var reach = Math.max(meta.size_m[0], meta.size_m[1]) / 2;
  var FOV = 0.8, fresh = { az: -2.36, el: 0.62, dist: reach * 2.1 };
  var view = { az: fresh.az, el: fresh.el, dist: fresh.dist, stretch: 1, byHeight: 0 };
  function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

  function multiply(a, b) {
    var out = new Float32Array(16), r, c, k, s;
    for (c = 0; c < 4; c++) for (r = 0; r < 4; r++) {
      for (s = 0, k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      out[c * 4 + r] = s;
    }
    return out;
  }
  function matrix(aspect) {
    var zc = (meta.ground_m + meta.top_m) / 2;
    var ce = Math.cos(view.el);
    var eye = [view.dist * ce * Math.cos(view.az), view.dist * ce * Math.sin(view.az),
               zc + view.dist * Math.sin(view.el)];
    var f = [-eye[0], -eye[1], zc - eye[2]], len = Math.hypot(f[0], f[1], f[2]);
    var z = [-f[0] / len, -f[1] / len, -f[2] / len];
    var x = [-z[1], z[0], 0];                        // up (0, 0, 1) crossed with z
    var xl = Math.hypot(x[0], x[1]) || 1;
    x = [x[0] / xl, x[1] / xl, 0];
    var y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]];
    var dot = function (a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; };
    var look = new Float32Array([x[0], y[0], z[0], 0, x[1], y[1], z[1], 0,
                                 x[2], y[2], z[2], 0, -dot(x, eye), -dot(y, eye), -dot(z, eye), 1]);
    var near = view.dist / 50, far = view.dist * 6, t = 1 / Math.tan(FOV / 2);
    var lens = new Float32Array([t / aspect, 0, 0, 0, 0, t, 0, 0,
                                 0, 0, (far + near) / (near - far), -1,
                                 0, 0, 2 * far * near / (near - far), 0]);
    return multiply(lens, look);
  }

  var waiting = false;
  function draw() {
    if (waiting) return;
    waiting = true;
    requestAnimationFrame(function () {
      waiting = false;
      var ratio = window.devicePixelRatio || 1;
      var w = Math.round(canvas.clientWidth * ratio), h = Math.round(canvas.clientHeight * ratio);
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
      gl.viewport(0, 0, w, h);
      gl.clearColor(0.125, 0.141, 0.122, 1);
      gl.enable(gl.DEPTH_TEST);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.uniformMatrix4fv(u.m, false, matrix(w / h));
      gl.uniform1f(u.stretch, view.stretch);
      gl.uniform1f(u.ground, meta.ground_m);
      gl.uniform1f(u.top, Math.max(meta.plants_top_m, 0.5));
      gl.uniform1f(u.byHeight, view.byHeight);
      // A point as wide on screen as the little cube it stands for.
      gl.uniform1f(u.size, 1.5 * Math.max(meta.cube_m, 0.05) * h / (2 * Math.tan(FOV / 2)));
      gl.drawArrays(gl.POINTS, 0, n);
    });
  }

  var down = {}, pinch = null;
  canvas.addEventListener("pointerdown", function (e) {
    canvas.setPointerCapture(e.pointerId);
    down[e.pointerId] = [e.clientX, e.clientY];
  });
  canvas.addEventListener("pointermove", function (e) {
    var before = down[e.pointerId];
    if (!before) return;
    down[e.pointerId] = [e.clientX, e.clientY];
    var ids = Object.keys(down);
    if (ids.length === 1) {
      view.az -= (e.clientX - before[0]) * 0.008;
      view.el = clamp(view.el + (e.clientY - before[1]) * 0.006, 0.08, 1.55);
    } else if (ids.length === 2) {
      var a = down[ids[0]], b = down[ids[1]], gap = Math.hypot(a[0] - b[0], a[1] - b[1]);
      if (pinch) view.dist = clamp(view.dist * pinch / gap, reach * 0.08, reach * 5);
      pinch = gap;
    }
    draw();
  });
  function up(e) { delete down[e.pointerId]; pinch = null; }
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    view.dist = clamp(view.dist * Math.exp(e.deltaY * 0.0012), reach * 0.08, reach * 5);
    draw();
  }, { passive: false });

  var legend = box.querySelector(".legend");
  function press() {
    box.querySelectorAll("button").forEach(function (b) {
      var act = b.getAttribute("data-act");
      if (act === "photo") b.setAttribute("aria-pressed", String(!view.byHeight));
      if (act === "height") b.setAttribute("aria-pressed", String(!!view.byHeight));
      if (act === "stretch") b.setAttribute("aria-pressed", String(view.stretch > 1));
    });
    legend.style.visibility = view.byHeight ? "visible" : "hidden";
  }
  box.querySelector(".tools").addEventListener("click", function (e) {
    var act = e.target.getAttribute && e.target.getAttribute("data-act");
    if (act === "photo") view.byHeight = 0;
    if (act === "height") view.byHeight = 1;
    if (act === "stretch") view.stretch = view.stretch > 1 ? 1 : 3;
    if (act === "reset") { view.az = fresh.az; view.el = fresh.el; view.dist = fresh.dist; }
    press();
    draw();
  });
  window.addEventListener("resize", draw);
  press();
  draw();
})();
"""


def _(key: str, lang: str, **values: object) -> str:
    """One of this page's strings, in the farmer's language, with its blanks filled."""
    return text.pick(S[key], lang).format(**values)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _inline(path: Path) -> str | None:
    """A PNG as a data URI, so the saved file still shows it with no signal."""
    try:
        blob = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError as exc:
        log.warning("chart %s not readable: %s", path, exc)
        return None
    return "data:image/png;base64," + blob


def _inches(value: float | None, digits: int = 1) -> str:
    return "?" if value is None else f"{value:.{digits}f}"


# --------------------------------------------------------------------------- #
# The charts
# --------------------------------------------------------------------------- #


def draw_charts(item: FieldWater, out_dir: Path, *, banner: str | None = None) -> dict[str, str]:
    """The satellite and water figures as data URIs, skipping any that cannot be drawn.

    A missing chart is not an error: a field registered last week has no season
    to plot and no years of its own to plot it against. Each entry is either a
    data URI or the key of the sentence that goes where the picture would have
    been, so the page can say which of those happened.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    status, record = item.status, item.field
    images: dict[str, str] = {}
    if status is None:
        return images

    year = date.fromisoformat(status.as_of).year
    season = item.ndvi
    if season is not None and not season.empty:
        season = season[season["date"].map(lambda d: d.year) == year]
    if season is None or season.empty:
        images["ndvi"] = "no_season"
    elif item.baseline is None or item.baseline.empty:
        # The season on its own would be a green line with nothing to judge it
        # against, and the comparison is the whole point of the picture.
        images["ndvi"] = "no_normal"
    else:
        try:
            path = sat_charts.plot_field(
                field_id=record.id, name=record.name, crop=status.crop,
                index_name="NDVI", baseline=item.baseline, season=season, year=year,
                out_dir=out_dir, cutoff=date.fromisoformat(status.as_of), banner=banner,
            )
            images["ndvi"] = _inline(path) or "chart_failed"
        except Exception:            # a chart is never worth failing the page over
            log.exception("could not draw the NDVI chart for %s", record.id)
            images["ndvi"] = "chart_failed"

    if item.daily is None or item.daily.empty:
        images["water"] = "no_season"
    else:
        try:
            path = sat_charts.plot_water(
                status=status.to_dict(), daily=item.daily,
                projection=item.projection, out_dir=out_dir, banner=banner,
            )
            images["water"] = _inline(path) or "chart_failed"
        except Exception:
            log.exception("could not draw the water chart for %s", record.id)
            images["water"] = "chart_failed"
    return images




def _start_reason(status, lang: str) -> str:
    """Why the checkbook starts where it does, in the farmer's language.

    The checkbook names it in English for the team ("planted 2025-02-21"); the page
    already gives the date, so it only needs the kind of start.
    """
    reason = status.start_reason or ""
    key = ("reason_planted" if reason.startswith("planted") else
           "reason_irrigated" if reason.startswith("first irrigation") else "reason_assumed")
    return _(key, lang)


def draw_stage_chart(item: FieldWater, out_dir: Path, lang: str, *,
                     banner: str | None = None) -> str | None:
    """The sorghum heat chart as a data URI, or None when there is nothing to draw."""
    status = item.status
    if status is None or not status.stage or item.heat is None:
        return None
    stage = status.stage
    names = {key: text.pick(pair, lang) for key, pair in text.SORGHUM_STAGES.items()}
    maturity = text.pick(text.MATURITIES[stage["maturity"]], lang)
    try:
        path = sat_charts.plot_sorghum_stages(
            stage=stage, heat=item.heat, out_path=Path(out_dir) / f"stage_{lang}.png",
            names=names, title=_("chart_stage_title", lang, field=item.field.name),
            subtitle=_("chart_stage_sub", lang, maturity=maturity),
            today_label=_("chart_today", lang), critical_label=_("chart_critical", lang),
            projected_label=_("chart_projected", lang), axis_label=_("chart_axis", lang),
            banner=banner, month_names=list(text.MONTH_NAMES[lang]),
        )
    except Exception:            # a chart is never worth failing the page over
        log.exception("could not draw the stage chart for %s", item.field.id)
        return None
    return _inline(path)


def _sorghum(item: FieldWater, events: list[Event], image: str | None, lang: str,
             today: date) -> list[str]:
    """Where a sorghum field is, what comes next, what to check, and the counts sent in."""
    status = item.status
    if status is None or not status.stage:
        return []
    stage = status.stage
    name = lambda key: text.pick(text.SORGHUM_STAGES[key], lang)  # noqa: E731
    parts = [f"<h2>{_esc(_('sorghum', lang))}</h2>",
             f"<p>{_esc(_('sorghum_body', lang, field=item.field.name, stage=name(stage['stage']), day=stage['days_after_planting'], gdu=stage['gdu']))}</p>"]
    if stage["maturity_assumed"]:
        parts.append(f"<p>{_esc(_('sorghum_maturity_assumed', lang))}</p>")
    else:
        maturity = text.pick(text.MATURITIES[stage["maturity"]], lang)
        parts.append(f"<p>{_esc(_('sorghum_maturity', lang, maturity=maturity))}</p>")
    parts.append(f"<p>{_esc(_('sorghum_critical', lang))}</p>")
    parts.append(f'<figure><img src="{image}" alt=""></figure>' if image else
                 f'<p class="note">{_esc(_("sorghum_no_chart", lang))}</p>')

    rows = []
    shown = ("emergence", "five_leaf", "panicle_initiation", "boot", "heading", "flowering",
             "soft_dough", "hard_dough", "black_layer")
    for milestone in stage["milestones"]:
        key = milestone["stage"]
        if key not in shown or not milestone["date"]:
            continue
        day = date.fromisoformat(milestone["date"])
        when = _("reached", lang) if milestone["gdu"] <= stage["gdu"] else _("expected", lang)
        watch = ", ".join(text.pick(WATCH_WORDS[w], lang) for w in sat_stages.watch_for(key))
        current = ' class="now"' if key == stage["stage"] else ""
        rows.append(f"<tr{current}><td>{_esc(name(key))}</td>"
                    f"<td>{_esc(when)} {_esc(text.day(day, lang, today))}</td>"
                    f"<td>{_esc(watch)}</td></tr>")
    parts.append(f"<table><tr><th>{_esc(_('stage_col', lang))}</th>"
                 f"<th>{_esc(_('date_col', lang))}</th><th>{_esc(_('watch_col', lang))}</th></tr>"
                 + "".join(rows) + "</table>")

    parts.append(f"<h2>{_esc(_('pests', lang))}</h2>")
    for key in ("pest_aphid", "pest_midge", "pest_heads", "pest_advice"):
        parts.append(f"<p>{_esc(_(key, lang))}</p>")

    parts.append(f"<h2>{_esc(_('counts', lang))}</h2>")
    counts = [e for e in events if e.kind == "scouting" and e.voided_at is None]
    if not counts:
        parts.append(f"<p>{_esc(_('counts_empty', lang))}</p>")
        return parts
    rows = []
    for event in sorted(counts, key=lambda e: e.day, reverse=True)[:12]:
        try:
            note = json.loads(event.note or "{}")
        except ValueError:
            note = {}
        seen = (f"{note['infested']} / {note['checked']}"
                if note.get("infested") is not None and note.get("checked") else "")
        verdict = _("verdict_" + note.get("verdict", "none"), lang)
        threshold = f" ({note['threshold']}%)" if note.get("threshold") else ""
        rows.append(f"<tr><td>{_esc(text.day(event.day, lang, today))}</td>"
                    f"<td class='n'>{_esc(round(note.get('percent', 0)))}%</td>"
                    f"<td class='n'>{_esc(seen)}</td>"
                    f"<td>{_esc(verdict + threshold)}</td></tr>")
    parts.append("<table>" + "".join(rows) + "</table>")
    return parts


def _drone_image(settings: Settings, report: dict | None, name: str) -> str | None:
    """A figure the drone half wrote beside its report, as a data URI."""
    if not report or not report.get("flight_id"):
        return None
    path = settings.drone_workspace / "out" / report["flight_id"] / f"{name}.png"
    return _inline(path) if path.exists() else None


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


def build(settings: Settings, farmer: Farmer, item: FieldWater, events: list[Event], *,
          today: date, terrain: dict | None = None, thermal: dict | None = None,
          flags: dict | None = None, download: str | None = None,
          advice=None) -> str:
    """One field's explanation as a single HTML page."""
    lang = farmer.language
    record, status = item.field, item.status
    images = draw_charts(item, settings.sms_dir / "explain" / record.id,
                         banner=settings.banner)
    parts: list[str] = []
    if settings.banner:
        parts.append(f'<div class="banner">{_esc(settings.banner)}</div>')
    parts.append("<main>")
    parts.append(f"<h1>{_esc(_('title', lang, field=record.name))}</h1>")
    parts.append(f'<p class="made">'
                 f"{_esc(_('made', lang, day=text.day(today, lang), name=farmer.name or ''))}"
                 f"</p>")
    if download:
        parts.append(f'<a class="save" href="{_esc(download)}" download>'
                     f"{_esc(_('save', lang))}</a>")

    parts += _ai_advice(item, advice, lang, today) if advice is not None else \
        _answer(item, lang, today)
    # A flight's picture of where to walk sits right under the answer: of
    # everything on this page it is the one a farmer acts on first.
    ai = _trees_ai(settings, flags, lang, today)
    parts += ai or _found(settings, flags, lang, today)
    parts += _model3d(settings, flags or terrain, lang, today)
    parts += _arithmetic(item, events, lang)
    parts += _charts(item, images, lang)
    stage_image = draw_stage_chart(item, settings.sms_dir / "explain" / record.id, lang,
                                   banner=settings.banner)
    parts += _sorghum(item, events, stage_image, lang, today)
    parts += _log(events, lang, today)
    parts += _ground(settings, record, terrain, lang)
    parts += _flight(settings, thermal or terrain, lang)
    parts += _thermal(settings, thermal, lang)
    parts += _sources(lang, sorghum=bool(status is not None and status.stage),
                      ground_3dep=(terrain or {}).get("ground_source") == "3dep")

    parts.append(f"<footer>{_esc(_('footer', lang))}</footer>")
    parts.append("</main>")
    body = "\n".join(parts)
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_esc(_('title', lang, field=record.name))}</title>"
            f"<style>{STYLE}</style></head><body>{body}</body></html>")


def _answer(item: FieldWater, lang: str, today: date) -> list[str]:
    """The same sentence the text message said, at the top, so the two can be checked."""
    from .status import message as status_message

    urgent = item.status is not None and item.status.days_left == 0
    return [f"<h2>{_esc(_('answer', lang))}</h2>",
            f'<div class="answer{" now" if urgent else ""}">'
            f"{_esc(status_message(item, lang, today))}</div>"]


def _ai_advice(item: FieldWater, advice, lang: str, today: date) -> list[str]:
    """The AI's recommendation, why, how it was checked, then the checkbook's own answer."""
    from .bot import ai_text
    from .status import message as status_message

    urgent = advice.action == "water_now"
    parts = [f"<h2>{_esc(_('ai_title', lang))}</h2>",
             f'<div class="answer{" now" if urgent else ""}">'
             f"{_esc(ai_text(item.field.name, advice, lang).split(' (')[0])}</div>"]
    if advice.reasons:
        parts.append(f"<p>{_esc(_('ai_why', lang))}</p><ul>"
                     + "".join(f"<li>{_esc(r)}</li>" for r in advice.reasons) + "</ul>")
    confidence = text.pick(AI_CONFIDENCE.get(advice.confidence, AI_CONFIDENCE["medium"]), lang)
    parts.append(f'<p class="note">{_esc(_("ai_how", lang, model=advice.model, checks="; ".join(advice.checked), confidence=confidence))}</p>')
    parts.append(f"<p>{_esc(_('ai_book', lang))}</p>")
    parts.append(f'<div class="answer">{_esc(status_message(item, lang, today))}</div>')
    return parts


def _arithmetic(item: FieldWater, events: list[Event], lang: str) -> list[str]:
    """The four steps of the checkbook, each with this field's own numbers."""
    s = item.status
    if s is None:
        return []
    parts: list[str] = [f"<h2>{_esc(_('how', lang))}</h2>"]

    def step(heading: str, body: str, **values) -> None:
        parts.append(f"<h3>{_esc(_(heading, lang))}</h3>")
        parts.append(f"<p>{_esc(_(body, lang, **values))}</p>")

    step("step_soil", "step_soil_body",
         field=item.field.name,
         soil=(item.soil or {}).get("name") or (s.soil or {}).get("name") or "?",
         root=_inches(s.root_depth_in, 0), capacity=_inches(s.capacity_in),
         stress=_inches(s.stress_point_in))

    start = date.fromisoformat(s.start)
    went_in = {"irrigated": 0.0, "rain": 0.0}
    for event in events:
        if event.kind in went_in and event.voided_at is None and event.day >= start:
            # An irrigation with no depth given still happened; the checkbook takes
            # it as a full refill, so no total here could be right either way. It
            # stays out of the sum and shows in the table of dates below.
            went_in[event.kind] += event.inches or 0.0
    step("step_in", "step_in_body", start=text.day(start, lang),
         start_reason=_start_reason(s, lang), irrigation=_inches(went_in["irrigated"]),
         rain=_inches(went_in["rain"]))

    step("step_out", "step_out_body", eto=_inches(s.eto_in_day, 2),
         kc=f"{s.kc:.2f}" if s.kc else "?", use=_inches(s.use_in_day, 2))

    if s.days_left is None:
        step("step_left", "step_left_plenty", left=_inches(s.until_stress_in),
             horizon=sat_water.MAX_PROJECTION_DAYS)
    else:
        low, high = s.days_range or (s.days_left, s.days_left)
        step("step_left", "step_left_body", left=_inches(s.until_stress_in),
             use=_inches(s.use_in_day, 2), days=text.about_days(s.days_left, lang),
             low=low, high=high)
    return parts


def _charts(item: FieldWater, images: dict[str, str], lang: str) -> list[str]:
    span = item.baseline_years
    years = (f"{span[0]}-{span[1]}" if span else
             text.pick(("sus años anteriores", "its earlier years"), lang))
    parts = [f"<h2>{_esc(_('chart_ndvi', lang))}</h2>",
             f"<p>{_esc(_('chart_ndvi_body', lang, field=item.field.name, years=years))}</p>"]
    parts.append(_figure(images.get("ndvi"), lang))
    parts.append(f"<h2>{_esc(_('chart_water', lang))}</h2>")
    parts.append(f"<p>{_esc(_('chart_water_body', lang))}</p>")
    parts.append(_figure(images.get("water"), lang))
    return parts


def _figure(source: str | None, lang: str) -> str:
    """The picture, or, in its place, the sentence saying why there isn't one."""
    if not source:
        source = "chart_failed"
    if not source.startswith("data:"):
        return f'<p class="note">{_esc(_(source, lang))}</p>'
    return f'<figure><img src="{source}" alt=""></figure>'


def _log(events: list[Event], lang: str, today: date) -> list[str]:
    """Every date the sums used, so a wrong one is easy to spot and correct."""
    shown = [e for e in events if e.kind in SHOWN_KINDS and e.voided_at is None]
    parts = [f"<h2>{_esc(_('log', lang))}</h2>"]
    if not shown:
        parts.append(f"<p>{_esc(_('log_empty', lang))}</p>")
        return parts
    parts.append(f"<p>{_esc(_('log_body', lang))}</p>")
    rows = []
    for event in sorted(shown, key=lambda e: e.day, reverse=True)[:20]:
        kind = text.pick({"planted": ("siembra", "planted"),
                          "irrigated": ("riego", "watered"),
                          "rain": ("lluvia", "rain"),
                          "harvested": ("cosecha", "harvested")}[event.kind], lang)
        inches = "" if event.inches is None else f"{text.inches(event.inches)} in"
        rows.append(f"<tr><td>{_esc(text.day(event.day, lang, today))}</td>"
                    f"<td>{_esc(kind)}</td><td class='n'>{_esc(inches)}</td></tr>")
    parts.append("<table>" + "".join(rows) + "</table>")
    return parts


def _ground(settings: Settings, record: FieldRow, report: dict | None, lang: str) -> list[str]:
    if not report:
        return []
    year = str(report.get("flown_on") or "")[:4] or "?"
    # Three ways the ground gets measured, and a grower should know which: the
    # government's public survey, their own flight's laser, or its photographs.
    who = {"3dep": "ground_lidar", "lidar": "ground_cloud"}.get(
        report.get("ground_source"), "ground_drone")
    parts = [f"<h2>{_esc(_('ground', lang))}</h2>",
             f"<p>{_esc(_(who, lang, field=record.name, year=year))}</p>",
             f"<p>{_esc(_('ground_why', lang))}</p>"]
    image = _drone_image(settings, report, "terrain")
    if image:
        parts.append(f'<figure><img src="{image}" alt=""></figure>')
    todo = [item for item in report.get("advice") or [] if item.get("priority") == 1]
    if todo:
        parts.append("<ul>" + "".join(
            f"<li>{_esc(item.get('finding', ''))} {_esc(item.get('advice', ''))}</li>"
            for item in todo[:4]) + "</ul>")
    return parts


def _flight(settings: Settings, report: dict | None, lang: str) -> list[str]:
    """The pictures only a farmer's own flight can give: canopy height and the flags.

    Nothing here exists for a satellite-only field, and nothing on the page
    depends on it.
    """
    parts: list[str] = []
    for name, heading, body in (("chm", "canopy", "canopy_body"),):
        image = _drone_image(settings, report, name)
        if image:
            parts.append(f"<h2>{_esc(_(heading, lang))}</h2>")
            parts.append(f"<p>{_esc(_(body, lang))}</p>")
            parts.append(f'<figure><img src="{image}" alt=""></figure>')
    return parts


def _found(settings: Settings, summary: dict | None, lang: str, today: date) -> list[str]:
    """What the latest flight flagged, with its picture, right under the answer."""
    image = _drone_image(settings, summary, "flag_overlay")
    if not summary or not image:
        return []
    stressed = (summary.get("n_stressed") or 0) + (summary.get("n_dead") or 0)
    missing = summary.get("n_missing") or 0
    unit = summary.get("unit_type")
    key = {"cell": "found_cells", "crown": "found_trees"}.get(unit, "found_rows")
    flown = summary.get("flown_on")
    day = text.day(date.fromisoformat(flown), lang, today) if flown else "?"
    line = _(key, lang, problem=f"{stressed + missing:,}", total=f"{summary.get('n_judged') or 0:,}",
             stressed=f"{stressed:,}", missing=f"{missing:,}",
             cell=f"{summary.get('cell_m') or 1:g}", day=day)
    parts = [f"<h2>{_esc(_('found', lang))}</h2>", f"<p>{_esc(line)}</p>",
             f'<figure><img src="{image}" alt=""></figure>',
             f"<p>{_esc(_('found_key', lang))}</p>"]
    if unit == "cell":
        parts.append(f'<p class="note">{_esc(_("found_colour", lang))}</p>')
    return parts


def trees_ai_report(settings: Settings, report: dict | None) -> dict | None:
    """The trained model's count for the flight a report came from, if it ran."""
    if not report or not report.get("flight_id"):
        return None
    path = settings.drone_workspace / "out" / report["flight_id"] / "trees_ai.json"
    if not path.exists():
        return None
    return {**json.loads(path.read_text("utf-8")), "flight_id": report["flight_id"],
            "flown_on": report.get("flown_on")}


def _trees_ai(settings: Settings, flags: dict | None, lang: str, today: date) -> list[str]:
    """An orchard's trees as the trained model counted them, and how far to trust it."""
    summary = trees_ai_report(settings, flags)
    image = _drone_image(settings, summary, "trees_ai")
    if not summary or not image:
        return []
    flown = summary.get("flown_on")
    height = (summary.get("height_m") or {}).get("median")
    body = _("trees_ai_body", lang, trees=f"{summary['trees']:,}", rows=summary["rows"],
             look=f"{summary['needs_a_look']:,}", gaps=f"{summary['gaps']:,}",
             height=f"{height:.1f}" if height else "?",
             day=text.day(date.fromisoformat(flown), lang, today) if flown else "?")
    if summary.get("not_judged"):
        body += _("trees_ai_edge", lang, n=summary["not_judged"])
    parts = [f"<h2>{_esc(_('trees_ai', lang))}</h2>", f"<p>{_esc(body)}</p>",
             f'<figure><img src="{image}" alt=""></figure>',
             f"<p>{_esc(_('trees_ai_how', lang))}</p>"]
    scores = (summary.get("model") or {}).get("scores") or {}
    if scores:
        def span(key, scale=100, unit="%"):
            values = [s[key] * scale for s in scores.values() if s.get(key) is not None]
            low, high = min(values), max(values)
            return (f"{low:.0f}{unit}" if round(low) == round(high)
                    else f"{low:.0f}-{high:.0f}{unit}")

        def fraction(key):
            return " / ".join(dict.fromkeys(s[key].replace(" of ", "/") for s in scores.values()
                                            if s.get(key)))

        parts.append(f'<p class="note">{_esc(_("trees_ai_score", lang, found=span("trees_found"), old=span("watershed_found"), miss=span("height_typical_miss_m", 100, ""), old_miss=span("canopy_model_typical_miss_m", 100, ""), dead=fraction("empty_spots_found_as_gaps"), poor=fraction("poor_living_trees_caught")))}</p>')
    parts.append(f'<p class="note">{_esc(_("trees_ai_caution", lang))}</p>')
    return parts


def _model3d(settings: Settings, report: dict | None, lang: str, today: date) -> list[str]:
    """The flight's 3D model, to turn with a finger; its snapshot where WebGL is missing."""
    if not report or not report.get("flight_id"):
        return []
    out = settings.drone_workspace / "out" / report["flight_id"]
    meta_path, data_path = out / "model3d.json", out / "model3d.bin"
    if not (meta_path.exists() and data_path.exists()):
        return []
    meta = json.loads(meta_path.read_text("utf-8"))
    keep = {k: meta[k] for k in ("points", "scale_m", "size_m", "ground_m", "top_m",
                                 "plants_top_m", "cube_m")}
    photos = settings.drone_workspace / "data" / "odm" / report["flight_id"] / "images"
    count = sum(1 for p in photos.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".tif",
                                                                     ".tiff", ".png")) \
        if photos.is_dir() else 0
    flown = report.get("flown_on")
    body = _("model3d_body", lang, photos=f"{count:,}" if count else "",
             x=f"{meta['size_m'][0]:.0f}", y=f"{meta['size_m'][1]:.0f}",
             top=f"{meta['plants_top_m']:.1f}",
             day=text.day(date.fromisoformat(flown), lang, today) if flown else "?")
    points = base64.b64encode(data_path.read_bytes()).decode("ascii")
    snapshot = _drone_image(settings, report, "model3d")
    buttons = "".join(f'<button type="button" data-act="{act}">{_esc(_(key, lang))}</button>'
                      for act, key in (("photo", "model3d_photo"), ("height", "model3d_height"),
                                       ("stretch", "model3d_stretch"), ("reset", "model3d_reset")))
    fallback = (f'<div class="fallback"><p class="note">{_esc(_("model3d_nogl", lang))}</p>'
                f'<img src="{snapshot}" alt=""></div>' if snapshot else "")
    return [
        f"<h2>{_esc(_('model3d', lang))}</h2>",
        f"<p>{_esc(body.replace('  ', ' '))}</p>",
        f'<div class="m3d" data-meta="{_esc(json.dumps(keep))}">',
        '<canvas aria-label="3D"></canvas>',
        f'<div class="tools">{buttons}</div>',
        f'<div class="legend">{_esc(_("model3d_legend", lang))} 0 m <i></i> '
        f'{meta["plants_top_m"]:.1f} m</div>',
        f'<p class="note">{_esc(_("model3d_how", lang))}</p>',
        fallback,
        f'<script type="application/octet-stream" class="points">{points}</script>',
        "</div>",
        f"<script>{VIEWER_JS}</script>",
    ]


def _thermal(settings: Settings, report: dict | None, lang: str) -> list[str]:
    if not report:
        return []
    parts = [f"<h2>{_esc(_('thermal', lang))}</h2>",
             f"<p>{_esc(_('thermal_body', lang))}</p>"]
    image = _drone_image(settings, report, "thermal")
    if image:
        parts.append(f'<figure><img src="{image}" alt=""></figure>')
    if (report.get("warm_share") or 0) > 0.35:
        share = f"{report['warm_share'] * 100:.0f}"
        parts.append(f'<p class="note">{_esc(_("thermal_all_hot", lang, share=share))}</p>')
    patches = sorted(report.get("patches") or [], key=lambda p: p.get("chance", 0), reverse=True)
    if not patches:
        parts.append(f"<p>{_esc(_('thermal_none', lang))}</p>")
        return parts + _thermal_limits(report, lang)
    for patch in patches:
        where = text.pick(text.PLACES.get(patch.get("where") or "middle",
                                          ("en el centro", "in the middle")), lang)
        head = _("thermal_patch", lang, chance=f"{patch.get('chance', 0) * 100:.0f}",
                 where=where, area=f"{patch.get('area_m2', 0):,.0f}",
                 above=f"{patch.get('above_c', 0):+.1f}")
        signs = "".join(f"<li>{_esc(line)}</li>" for line in _signs(patch, lang))
        parts.append(f'<div class="patch"><div class="chance">{_esc(head)}</div>'
                     f"<ul>{signs}</ul>"
                     f'<p class="note">{_esc(_("thermal_go", lang))}</p></div>')
    return parts + _thermal_limits(report, lang)


def _thermal_limits(report: dict, lang: str) -> list[str]:
    """What the camera could not do, in the farmer's language.

    The drone half writes its own notes in English for whoever runs it; these
    are the two that change how much weight a grower should put on the answer,
    so they are said here rather than pasted.
    """
    lines = []
    if report.get("leaves_from") == "heat":
        lines.append(_("thermal_by_heat", lang))
    scan = report.get("scan") or {}
    if scan.get("minutes") and scan.get("drift_c"):
        lines.append(_("thermal_scan", lang, minutes=f"{scan['minutes']:.0f}",
                       drift=f"{scan['drift_c']:.1f}"))
    return [f'<p class="note">{_esc(line)}</p>' for line in lines]


def _signs(patch: dict, lang: str) -> list[str]:
    """One patch's signs in the farmer's language, falling back to what was written.

    The drone half writes both the prose and the key that produced it. A key
    this page has not learned yet shows in English rather than disappearing.
    """
    prose = patch.get("signs") or []
    keys = patch.get("sign_keys") or []
    if not keys:
        return list(prose)
    values = {
        "above_c": f"{patch.get('above_c', 0):+.1f}",
        "area": f"{patch.get('area_m2', 0):,.0f}",
        "share": f"{(patch.get('problem_share') or 0) * 100:.0f}%",
        "field": f"{(patch.get('field_share') or 0) * 100:.0f}%",
    }
    lines = []
    for index, key in enumerate(keys):
        pair = SIGNS.get(key)
        if pair is None:
            lines.append(prose[index] if index < len(prose) else key)
        else:
            lines.append(text.pick(pair, lang).format(**values))
    return lines


def _sources(lang: str, *, sorghum: bool = False, ground_3dep: bool = False) -> list[str]:
    listed = (SOURCES + (SORGHUM_SOURCES if sorghum else [])
              + ([GROUND_3DEP_SOURCE] if ground_3dep else []))
    rows = "".join(f"<tr><td>{_esc(name)}</td><td>{_esc(text.pick(what, lang))}</td></tr>"
                   for name, what in listed)
    return [f"<h2>{_esc(_('sources', lang))}</h2>", f"<table>{rows}</table>",
            f"<h2>{_esc(_('limits', lang))}</h2>", f"<p>{_esc(_('limits_body', lang))}</p>"]
