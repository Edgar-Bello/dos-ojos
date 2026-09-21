"""Build the Dos Ojos demo presentation.

Run from any folder:
    python C:/Users/edgar/Projects/Dos_Ojos/presentation/build_deck.py

Every chat bubble is the simulator's own message text (assets/chats.json), and every
screenshot came from the running demo. The English under a Spanish bubble is the
chatbot's own English template for the same message, filled with the same values.
Public data keeps its label on every picture; the thermal picture says it is synthetic.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
OUT = HERE / "Dos_Ojos_demo.pptx"

# --------------------------------------------------------------------------- #
# Look
# --------------------------------------------------------------------------- #

INK = "1E2B24"      # dark olive-charcoal: dark slides, titles
PAPER = "FFFFFF"
MIST = "EDF1EC"     # green-leaning light grey: panels
FIELD = "2E6B45"    # field green: the satellite eye
WATER = "157A8C"    # irrigation teal: text messages, water
RUST = "B8472A"     # sorghum-head rust: the drone eye, "look here"
MUTED = "5F6B64"
LINE = "D5DCD6"
ON_DARK = "F2F5F1"
ON_DARK_MUTED = "A9B8AE"

TITLE_FONT = "Century Schoolbook"
BODY_FONT = "Calibri"

W, H = 13.333, 7.5
M = 0.6                          # side margin
CONTENT_TOP = 1.75


def rgb(hex_colour: str) -> RGBColor:
    return RGBColor.from_string(hex_colour)


prs = Presentation()
prs.slide_width = Inches(W)
prs.slide_height = Inches(H)
BLANK = prs.slide_layouts[6]
SLIDE_NO = 0


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #

def new_slide(dark: bool = False):
    global SLIDE_NO
    SLIDE_NO += 1
    slide = prs.slides.add_slide(BLANK)
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = rgb(INK if dark else PAPER)
    if not dark:
        number = slide.shapes.add_textbox(Inches(W - M - 0.6), Inches(H - 0.5),
                                          Inches(0.6), Inches(0.3))
        para = number.text_frame.paragraphs[0]
        para.alignment = PP_ALIGN.RIGHT
        run = para.add_run()
        run.text = str(SLIDE_NO)
        style(run, 10, MUTED)
    return slide


def style(run, size, colour, *, bold=False, italic=False, font=BODY_FONT):
    run.font.size = Pt(size)
    run.font.color.rgb = rgb(colour)
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = font


def shape(slide, kind, x, y, w, h, fill=None, line=None, radius=None):
    item = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    item.shadow.inherit = False
    if fill:
        item.fill.solid()
        item.fill.fore_color.rgb = rgb(fill)
    else:
        item.fill.background()
    if line:
        item.line.color.rgb = rgb(line)
        item.line.width = Pt(1.25)
    else:
        item.line.fill.background()
    if radius is not None:
        item.adjustments[0] = radius
    return item


def text(slide, x, y, w, h, paragraphs, *, anchor=MSO_ANCHOR.TOP, margin=0.0):
    """paragraphs: list of (runs, options); runs: list of (text, size, colour, dict)."""
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(frame, side, Inches(margin))
    for index, (runs, options) in enumerate(paragraphs):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.alignment = options.get("align", PP_ALIGN.LEFT)
        para.space_after = Pt(options.get("after", 0))
        para.line_spacing = options.get("spacing", 1.05)
        for words, size, colour, extra in runs:
            run = para.add_run()
            run.text = words
            style(run, size, colour, **extra)
    return box


def para(words, size, colour, after=0, align=PP_ALIGN.LEFT, spacing=1.05, **extra):
    return ([(words, size, colour, extra)], {"after": after, "align": align,
                                             "spacing": spacing})


def title(slide, words, *, sub=None, dark=False):
    text(slide, M, 0.55, 9.6, 0.95,
         [para(words, 34, ON_DARK if dark else INK, bold=True, font=TITLE_FONT)],
         anchor=MSO_ANCHOR.TOP)
    if sub:
        text(slide, M, 1.18, 10.0, 0.45,
             [para(sub, 16, ON_DARK_MUTED if dark else MUTED)])


def eyes(slide, *, satellite=False, drone=False, x=W - M - 0.92, y=0.66, d=0.4, dark=False):
    """The Dos Ojos mark: left circle the satellite, right the drone; filled = in play."""
    for offset, on, colour in ((0.0, satellite, FIELD), (d + 0.12, drone, RUST)):
        shape(slide, MSO_SHAPE.OVAL, x + offset, y, d, d,
              fill=colour if on else None,
              line=None if on else (ON_DARK_MUTED if dark else LINE))


def pill(slide, words, x, y, colour=RUST, w=1.45):
    tag = shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, 0.36, fill=colour, radius=0.5)
    frame = tag.text_frame
    frame.margin_left = frame.margin_right = Inches(0.05)
    frame.margin_top = frame.margin_bottom = Inches(0)
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = frame.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = words
    style(run, 11, PAPER, bold=True)
    run.font.name = BODY_FONT
    return tag


def picture(slide, name, x, y, w, h, *, align="center", border=True):
    """Fit a picture inside a box without stretching it; returns its placed box."""
    path = ASSETS / name
    iw, ih = Image.open(path).size
    scale = min(w / iw, h / ih)
    pw, ph = iw * scale, ih * scale
    px = x + (w - pw) / 2 if align == "center" else (x if align == "left" else x + w - pw)
    py = y
    placed = slide.shapes.add_picture(str(path), Inches(px), Inches(py), Inches(pw), Inches(ph))
    if border:
        placed.line.color.rgb = rgb(LINE)
        placed.line.width = Pt(0.75)
    return px, py, pw, ph


def caption(slide, words, x, y, w, h=0.5, colour=MUTED, size=11):
    text(slide, x, y, w, h, [para(words, size, colour, spacing=1.0)])


def notes(slide, words):
    slide.notes_slide.notes_text_frame.text = words


def icon_row(slide, x, y, w, heading, body, colour, *, size=15, head=18, gap=0.18):
    """A small filled circle, a bold heading, and a line or two beneath it."""
    shape(slide, MSO_SHAPE.OVAL, x, y + 0.06, 0.26, 0.26, fill=colour)
    text(slide, x + 0.45, y, w - 0.45, 1.6,
         [para(heading, head, INK, after=3, bold=True),
          para(body, size, MUTED, spacing=1.08)])


# --------------------------------------------------------------------------- #
# Chat bubbles, sized from the real message text
# --------------------------------------------------------------------------- #

EM = 0.47          # Calibri's average advance, as a share of the point size


def wrapped_lines(words: str, width_in: float, size: float) -> int:
    per_line = max(6, int(width_in / (EM * size / 72)))
    lines = 0
    for block in words.split("\n"):
        count, used = 1, 0
        for word in block.split(" "):
            length = len(word)
            if length > per_line:                      # a link breaks mid-word
                extra = math.ceil(length / per_line) - 1
                count += extra + (1 if used else 0)
                used = length % per_line
                continue
            if used == 0:
                used = length
            elif used + 1 + length <= per_line:
                used += 1 + length
            else:
                count += 1
                used = length
        lines += count
    return lines


def chat(slide, messages, x, y, w, h, *, who, pad=0.22, size=14.0, floor=10.5):
    """A chat panel. messages: dicts with dir ('in' = the farmer), body, and optional en."""
    header = 0.55
    inner = w - 2 * pad
    max_bubble = inner * 0.84

    def layout(pt):
        tr_pt = pt - 2.5
        line_h = pt * 1.22 / 72
        tr_line_h = tr_pt * 1.2 / 72
        placed, cursor = [], 0.0
        for message in messages:
            # Short replies are mostly capitals and digits, which run wider than average.
            em = 0.64 if len(message["body"]) <= 12 else EM
            natural = len(message["body"]) * em * pt / 72
            bubble_w = min(max_bubble, max(0.9, natural + 0.45))
            lines = wrapped_lines(message["body"], bubble_w - 0.26, pt)
            bubble_h = lines * line_h + 0.2
            tr_h = 0.0
            if message.get("en"):
                tr_lines = wrapped_lines(message["en"], max_bubble - 0.05, tr_pt)
                tr_h = tr_lines * tr_line_h + 0.06
            placed.append((message, bubble_w, bubble_h, tr_h))
            cursor += bubble_h + tr_h + 0.16
        return placed, cursor - 0.16, pt, tr_pt

    pt = size
    placed, used, pt, tr_pt = layout(pt)
    while used > h - header - 2 * pad and pt > floor:
        pt -= 0.5
        placed, used, pt, tr_pt = layout(pt)

    shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill=MIST, radius=0.04)
    shape(slide, MSO_SHAPE.OVAL, x + pad, y + 0.16, 0.34, 0.34, fill=FIELD)
    text(slide, x + pad + 0.46, y + 0.12, inner - 0.5, 0.45,
         [([("Dos Ojos", 13, INK, {"bold": True}),
            ("   text messages with " + who, 11, MUTED, {})], {})],
         anchor=MSO_ANCHOR.MIDDLE)

    cursor = y + header + pad * 0.6
    for message, bubble_w, bubble_h, tr_h in placed:
        farmer = message["dir"] == "in"
        bx = x + w - pad - bubble_w if farmer else x + pad
        bubble = shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, bx, cursor, bubble_w, bubble_h,
                       fill=WATER if farmer else PAPER, radius=min(0.5, 0.12 / bubble_h * 1.6))
        frame = bubble.text_frame
        frame.word_wrap = True
        frame.margin_left = frame.margin_right = Inches(0.13)
        frame.margin_top = frame.margin_bottom = Inches(0.08)
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = frame.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        p.line_spacing = 1.0
        run = p.add_run()
        run.text = message["body"]
        style(run, pt, PAPER if farmer else INK)
        cursor += bubble_h
        if message.get("en"):
            tx = x + w - pad - (max_bubble) if farmer else x + pad + 0.02
            text(slide, tx, cursor + 0.03, max_bubble, tr_h,
                 [para(message["en"], tr_pt, MUTED, italic=True,
                       align=PP_ALIGN.RIGHT if farmer else PP_ALIGN.LEFT, spacing=1.0)])
            cursor += tr_h
        cursor += 0.16
    return used


CHATS = json.loads((ASSETS / "chats.json").read_text(encoding="utf-8"))


def with_english(section, translations):
    return [dict(m, en=translations.get(i)) for i, m in enumerate(CHATS[section])]


# --------------------------------------------------------------------------- #
# 1  Title
# --------------------------------------------------------------------------- #

s = new_slide(dark=True)
shape(s, MSO_SHAPE.OVAL, M, 1.25, 1.35, 1.35, fill=FIELD)
shape(s, MSO_SHAPE.OVAL, M + 1.55, 1.25, 1.35, 1.35, fill=RUST)
text(s, M, 2.95, 11.5, 1.3, [para("Dos Ojos", 72, ON_DARK, bold=True, font=TITLE_FONT)])
text(s, M, 4.2, 11.5, 0.7, [para("Two eyes on every field", 30, ON_DARK_MUTED, font=TITLE_FONT)])
text(s, M, 5.05, 11.6, 0.9,
     [para("A text message that tells Rio Grande Valley farmers when to water, "
           "and where to look.", 20, ON_DARK)])
text(s, M, 6.55, 11.5, 0.4,
     [para("Satellite  ·  Drone  ·  Thermal camera  ·  Text messages in Spanish and English",
           13, ON_DARK_MUTED)])
notes(s, "Dos Ojos means 'two eyes'. One eye is a satellite that watches every field for "
         "free. The other is a drone, used only where it's needed. The farmer never opens an "
         "app: everything comes by text message, in Spanish or English. Add your name and "
         "the event here.")

# --------------------------------------------------------------------------- #
# 2  The problem
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "Farmers find out too late")
cards = [
    ("Water costs money",
     "Every watering costs water, fuel and a day's work. Too early wastes it. "
     "Too late costs the crop.", WATER, "drop"),
    ("Fields are too big to walk",
     "A problem in the middle of a big field can grow for weeks before anyone sees it "
     "from the road.", FIELD, "rows"),
    ("The tools don't fit",
     "Most farm software means an app, a password and a dashboard, usually in English. "
     "Every farmer already has text messages.", RUST, "phone"),
]
gap = 0.35
card_w = (W - 2 * M - 2 * gap) / 3
for i, (heading, body, colour, glyph) in enumerate(cards):
    cx = M + i * (card_w + gap)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, cx, 1.95, card_w, 4.75, fill=MIST, radius=0.05)
    shape(s, MSO_SHAPE.OVAL, cx + 0.4, 2.35, 0.95, 0.95, fill=colour)
    if glyph == "drop":
        drop = shape(s, MSO_SHAPE.TEAR, cx + 0.64, 2.6, 0.46, 0.46, fill=PAPER)
        drop.rotation = -45
    elif glyph == "rows":
        for k in range(3):
            shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, cx + 0.6 + k * 0.2, 2.57, 0.1, 0.52,
                  fill=PAPER, radius=0.5)
    else:
        shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, cx + 0.7, 2.52, 0.34, 0.6, line=PAPER, radius=0.18)
    text(s, cx + 0.4, 3.6, card_w - 0.8, 0.95,
         [para(heading, 24, INK, bold=True, font=TITLE_FONT, spacing=1.0)])
    text(s, cx + 0.4, 4.6, card_w - 0.75, 2.0, [para(body, 18, MUTED, spacing=1.12)])
notes(s, "Three problems. Water: watering at the wrong time wastes money or hurts the crop. "
         "Size: nobody can walk every acre every week. Tools: most agriculture technology "
         "assumes an app, a login and English. Text messages are the one tool every farmer "
         "already uses.")

# --------------------------------------------------------------------------- #
# 3  The idea
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "Two eyes, one text message")
shape(s, MSO_SHAPE.OVAL, 0.95, 2.1, 3.1, 3.1, fill=FIELD)
shape(s, MSO_SHAPE.OVAL, 4.35, 2.1, 3.1, 3.1, fill=RUST)
text(s, 0.95, 3.2, 3.1, 0.9, [para("Satellite", 26, PAPER, bold=True, font=TITLE_FONT,
                                   align=PP_ALIGN.CENTER)], anchor=MSO_ANCHOR.MIDDLE)
text(s, 4.35, 3.2, 3.1, 0.9, [para("Drone", 26, PAPER, bold=True, font=TITLE_FONT,
                                   align=PP_ALIGN.CENTER)], anchor=MSO_ANCHOR.MIDDLE)
col = 8.1
icon_row(s, col, 1.95, W - M - col, "Eye one: the satellite",
         "Looks at every field about every 5 days. It's free, and the farmer needs "
         "nothing at all.", FIELD)
icon_row(s, col, 3.45, W - M - col, "Eye two: the drone",
         "Zooms in only where it matters: plant by plant, the lie of the land, and with a "
         "thermal camera, hot spots that can mean pests.", RUST)
icon_row(s, col, 5.2, W - M - col, "The answer comes by text",
         "In Spanish or English, on any phone.", WATER)
caption(s, "The two circles in the corner of each slide show which eye is at work.",
        0.95, 5.55, 6.5, 0.5)
eyes(s, satellite=True, drone=True)
notes(s, "The satellite is the everyday eye: it covers every field for free, and most farmers "
         "will only ever use this. The drone is the zoom lens, called in when something needs "
         "a closer look. Both feed into the same thing: a plain text message.")

# --------------------------------------------------------------------------- #
# 4  How it works
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "How a text message gets made")
steps = [
    ("The satellite passes", "A fresh picture of every field, about every 5 days."),
    ("Compare the field with itself",
     "Is it as green as it usually is on this date? The yardstick is its own past years, "
     "never a neighbour's farm."),
    ("Keep a water checkbook",
     "Rain and every watering the farmer texts in go in. Sun and heat take water out."),
    ("Text the farmer",
     "When to water and how much. Reply WHY to see the working."),
]
gap = 0.5
step_w = (W - 2 * M - 3 * gap) / 4
for i, (heading, body) in enumerate(steps):
    sx = M + i * (step_w + gap)
    circle = shape(s, MSO_SHAPE.OVAL, sx, 1.95, 0.8, 0.8, fill=WATER if i == 3 else FIELD)
    frame = circle.text_frame
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = frame.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = str(i + 1)
    style(run, 24, PAPER, bold=True, font=TITLE_FONT)
    if i < 3:
        shape(s, MSO_SHAPE.CHEVRON, sx + step_w + gap / 2 - 0.12, 2.2, 0.26, 0.3, fill=LINE)
    text(s, sx, 3.0, step_w, 0.8,
         [para(heading, 19, INK, bold=True, font=TITLE_FONT, spacing=1.0)])
    text(s, sx, 3.85, step_w, 1.55, [para(body, 15, MUTED, spacing=1.1)])
shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, M, 5.5, W - 2 * M, 1.2, fill=MIST, radius=0.08)
shape(s, MSO_SHAPE.OVAL, M + 0.35, 5.93, 0.34, 0.34, fill=RUST)
text(s, M + 0.9, 5.62, W - 2 * M - 1.2, 0.95,
     [([("Only if the farmer wants more:  ", 16, INK, {"bold": True}),
        ("send drone photos for a plant-by-plant check, or add a thermal camera for an early "
         "pest warning.", 16, MUTED, {})], {"spacing": 1.1})],
     anchor=MSO_ANCHOR.MIDDLE)
eyes(s, satellite=True)
notes(s, "Four steps, all automatic. The key idea in step 2: we never judge a field against "
         "someone else's. Soils and varieties differ, so the only fair comparison is the "
         "field's own history. Step 3 is just bookkeeping: water in, water out. The drone and "
         "the thermal camera are extras on top.")

# --------------------------------------------------------------------------- #
# 5  Eye one: every field against its own past
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "Every field against its own past")
px, py, pw, ph = picture(s, "ndvi_corn.png", M, 1.6, W - 2 * M, 3.95)
caption(s, "Maiz Lyford, a real corn field near Lyford, Texas. Satellite: Sentinel-2 "
           "(Copernicus), free.", px, py + ph + 0.06, pw, h=0.35)
labels = [
    ("The green line", "How green the field is this season, measured from space.", FIELD),
    ("The grey band", "What this same field usually looks like on this date, in past years.",
     "9AA39D"),
    ("Below the band", "Behind its own normal: worth a closer look.", RUST),
]
gap = 0.35
label_w = (W - 2 * M - 2 * gap) / 3
for i, (heading, body, colour) in enumerate(labels):
    icon_row(s, M + i * (label_w + gap), 6.0, label_w, heading, body, colour, size=13.5,
             head=16)
eyes(s, satellite=True)
notes(s, "This is a real corn field. The green line is this year. The grey band is what this "
         "field normally does at this time of year. When the line drops below the band, the "
         "field is doing worse than it usually does. That's the signal to pay attention.")

# --------------------------------------------------------------------------- #
# 6  Water, kept like a checkbook
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "Water, kept like a checkbook")
px, py, pw, ph = picture(s, "water_corn.png", M, CONTENT_TOP, 8.55, 4.75, align="left")
caption(s, "Same corn field. Soil: USDA soil survey. Weather: gridMET, government data.",
        M, py + ph + 0.1, pw)
col = 9.55
icon_row(s, col, CONTENT_TOP, W - M - col, "The balance",
         "How much water this soil can hold for the roots.", WATER, size=15)
icon_row(s, col, CONTENT_TOP + 1.25, W - M - col, "Deposits",
         "Rain, plus every watering the farmer texts in.", FIELD, size=15)
icon_row(s, col, CONTENT_TOP + 2.5, W - M - col, "Withdrawals",
         "What sun and heat pull out each day, sized to how green the crop is.", RUST, size=15)
shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, col, CONTENT_TOP + 3.95, W - M - col, 1.05, fill=MIST,
      radius=0.1)
text(s, col + 0.25, CONTENT_TOP + 4.0, W - M - col - 0.45, 0.95,
     [para("Result: water by 25 May, about 4 inches.", 17, INK, bold=True)],
     anchor=MSO_ANCHOR.MIDDLE)
eyes(s, satellite=True)
notes(s, "Think of the soil like a bank account for water. Rain and irrigation are deposits. "
         "Every hot day is a withdrawal. When the balance gets low enough to hurt the crop, "
         "we tell the farmer to water, and roughly how much. The dotted line on the chart is "
         "what happens if it doesn't rain.")

# --------------------------------------------------------------------------- #
# 7  Demo divider
# --------------------------------------------------------------------------- #

s = new_slide(dark=True)
pill(s, "DEMO", M, 1.7, colour=RUST, w=1.1)
text(s, M, 2.3, 11.5, 1.2, [para("A week in the Valley", 58, ON_DARK, bold=True,
                                  font=TITLE_FONT)])
text(s, M, 3.65, 10.4, 1.6,
     [para("Juan, Maria and Pedro are made up. Their fields near Lyford, Elsa and Primera are "
           "real, and so is every satellite picture, weather reading and soil map behind their "
           "answers.", 21, ON_DARK, spacing=1.15)])
text(s, M, 5.45, 10.5, 0.8,
     [para("Every message and screen on the next slides comes from the working simulator.",
           16, ON_DARK_MUTED)])
eyes(s, satellite=True, drone=True, dark=True)
notes(s, "Now a demo. Juan, Maria and Pedro are made-up farmers, but their fields are real "
         "fields from public USDA maps, and the satellite and weather data behind their answers "
         "are real. Maria and Pedro grow grain sorghum. Every message you'll see was produced "
         "by the working system, not typed into the slides.")

# --------------------------------------------------------------------------- #
# 8  Demo 1: signing up
# --------------------------------------------------------------------------- #

s = new_slide()
pill(s, "DEMO  1 / 5", M, 0.12)
title(s, "Signing up is a text conversation")
signup = with_english("signup", {
    0: "hello",
    1: "What would you like to use? 1 Satellite only (you need nothing) 2 Satellite and your "
       "drone 3 Satellite, drone and thermal camera (pests). Change any time with PLAN.",
    3: "Note: to fly a drone over your farm the law asks for an FAA Part 107 licence ($175 "
       "exam). Up to you whether to get it, hire someone, or stay on satellite only.",
})
signup = [m for i, m in enumerate(signup) if i < 4]
chat(s, signup, M, CONTENT_TOP, 6.55, 5.2, who="Juan (made up)")
col = 7.55
text(s, col, CONTENT_TOP, W - M - col, 1.0,
     [para("No app, no password, any phone.", 22, INK, bold=True, font=TITLE_FONT)])
text(s, col, CONTENT_TOP + 1.0, W - M - col, 0.45,
     [para("Then it asks, one question at a time:", 16, MUTED)])
asks = ["What the field is called", "About how many acres", "Where it is: a map pin",
        "What's planted, and when", "How it's watered", "The last time it was watered"]
for i, item in enumerate(asks):
    yy = CONTENT_TOP + 1.5 + i * 0.44
    shape(s, MSO_SHAPE.OVAL, col, yy + 0.1, 0.16, 0.16, fill=FIELD)
    text(s, col + 0.32, yy, W - M - col - 0.32, 0.42, [para(item, 16, INK)])
shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, col, 5.95, W - M - col, 0.95, fill=MIST, radius=0.1)
text(s, col + 0.25, 6.0, W - M - col - 0.45, 0.85,
     [para("Option 1 needs nothing at all. Nobody is told they need a drone.", 15, INK,
           bold=True, spacing=1.05)], anchor=MSO_ANCHOR.MIDDLE)
eyes(s, satellite=True, drone=True)
notes(s, "Juan texts 'hola'. After his name and consent, he picks what he wants to use. Option "
         "1 needs nothing: satellite only. He picks 3, so the system warns him, honestly, "
         "that flying a drone needs a $175 FAA licence, and that it's his choice. Then it "
         "asks about each field, one question at a time.")

# --------------------------------------------------------------------------- #
# 9  Demo 2: AGUA
# --------------------------------------------------------------------------- #

s = new_slide()
pill(s, "DEMO  2 / 5", M, 0.12)
title(s, "One word, \"AGUA\", gets a plain answer")
water = with_english("water", {
    0: "Dos Ojos alert: Algodon Lyford will need water in 1 day, by Wed May 21. If you "
       "already watered, text WATERED and the date.",
    1: "WATER",
    2: "Algodon Lyford (cotton): water for 1 day (0 to 2); water by Wed May 21, about 3.7 "
       "inches by furrows. It's at bloom: don't let it dry out.",
    3: "Maiz Lyford (corn): water for about 5 days (3 to 7); water by Sun May 25, about 4.1 "
       "inches by furrows.",
})
water = [m for i, m in enumerate(water) if i < 4]
chat(s, water, M, CONTENT_TOP, 6.55, 5.2, who="Juan (made up)")
col = 7.55
icon_row(s, col, CONTENT_TOP + 0.05, W - M - col, "The alert came first",
         "Juan didn't have to ask. The cotton was a day from running dry.", RUST, size=15)
icon_row(s, col, CONTENT_TOP + 1.45, W - M - col, "Days left, with a range",
         "Weather is never certain, so every answer says how sure it is.", WATER, size=15)
icon_row(s, col, CONTENT_TOP + 2.85, W - M - col, "A date and an amount",
         "Water by this day, about this many inches, the way he already waters.", FIELD,
         size=15)
icon_row(s, col, CONTENT_TOP + 4.25, W - M - col, "Why it matters now",
         "The cotton is in bloom, when running dry hurts the most.", FIELD, size=15)
eyes(s, satellite=True)
notes(s, "First, an alert arrives on its own: the cotton will need water in a day. Juan texts "
         "AGUA, water, and gets both fields: how many days of water are left, a range because "
         "weather is uncertain, the date to water by, and about how many inches. The English "
         "under each bubble is what an English-speaking farmer would get.")

# --------------------------------------------------------------------------- #
# 10  Demo 3: PORQUE
# --------------------------------------------------------------------------- #

s = new_slide()
pill(s, "DEMO  3 / 5", M, 0.12)
title(s, "Reply \"PORQUE\" to see the working")
why = with_english("why", {
    0: "Want to know why? Reply WHY and I'll send you a file with the charts.",
    1: "WHY",
    2: "Why Maiz Lyford: [link] . It has the satellite charts, the water arithmetic and the "
       "ground. The link lasts 14 days.",
})
chat(s, why, M, CONTENT_TOP, 6.55, 3.55, who="Juan (made up)")
text(s, M, 5.55, 6.55, 1.4,
     [para("A farmer asked to open a valve on our say-so is owed the arithmetic.", 19, INK,
           after=6, bold=True, font=TITLE_FONT),
      para("It's always an offer, never a file nobody asked for.", 15, MUTED)])
px, py, pw, ph = picture(s, "why_page.png", 7.55, CONTENT_TOP - 0.1, W - M - 7.55, 4.85)
caption(s, "The page Juan opens: the same answer, the four steps with his field's own numbers, "
           "then the charts. It works saved or offline.", 7.55, py + ph + 0.1, W - M - 7.55,
        h=0.6, size=10.5)
eyes(s, satellite=True)
notes(s, "Every answer ends with an offer: want to know why? Reply PORQUE and you get a link to "
         "one page. It repeats the same answer, then shows the four steps with the field's own "
         "numbers: how much water the soil holds, what went in, what the sun took out, what's "
         "left. The charts are inside the file, so it works even without signal.")

# --------------------------------------------------------------------------- #
# 11  Demo 4: sorghum, its stage and its pests
# --------------------------------------------------------------------------- #

s = new_slide()
pill(s, "DEMO  4 / 5", M, 0.12)
title(s, "Sorghum: stage and scouting by text")
maria = [dict(m, en=e) for m, e in zip(CHATS["sorghum_maria"], (
    "Dos Ojos: Sorgo Elsa is at flowering. This week scout for sugarcane aphid and midge (the "
    "threshold is 1 per head) and text APHID with what you find.",
    "STAGE",
    "Sorgo Elsa: at flowering, day 69 after planting. Next: soft dough around Sat May 31. This "
    "stretch sets the yield. Check for midge every 3 days, 10 to 2: the threshold is 1 per "
    "head. Scout for sugarcane aphid weekly and text APHID. (Worked out as medium season; text "
    "MATURITY to change it.)"))]
left_w = 6.75
chat(s, maria, M, CONTENT_TOP, left_w, 5.2, who="Maria (made up), dryland near Elsa")
col = M + left_w + 0.4
pedro = [dict(m, en=e) for m, e in zip(CHATS["sorghum_pedro"], (
    "APHID 21 of 80",
    "Sorgo Primera: 26% of plants with aphids, close to the 30% threshold at soft dough. "
    "Check again in 3 or 4 days."))]
chat(s, pedro, col, CONTENT_TOP, W - M - col, 2.75, who="Pedro (made up), irrigated")
icon_row(s, col, CONTENT_TOP + 3.05, W - M - col, "The right threshold for the stage",
         "20% of plants before heading, 30% after. Past it: talk to your advisor today. No "
         "product is ever named.", RUST, size=13.5, head=16)
icon_row(s, col, CONTENT_TOP + 4.25, W - M - col, "No drone needed",
         "Both use the satellite only.", FIELD, size=13.5, head=16)
eyes(s, satellite=True)
notes(s, "Two sorghum growers. Maria farms dryland near Elsa. Every week while it matters she "
         "gets a reminder to scout, and when she asks ETAPA she hears where her crop is: "
         "flowering, day 69, what comes next and when, and what to look for right now. Pedro, "
         "near Primera, counted 21 plants with aphids out of 80: 26%, close to the 30% "
         "threshold for his stage, so he's told to look again in a few days. Neither has a "
         "drone.")

# --------------------------------------------------------------------------- #
# 11  Demo 4: drone photos
# --------------------------------------------------------------------------- #

s = new_slide()
pill(s, "DEMO  5 / 5", M, 0.12)
title(s, "Drone photos go in through a link")
chat(s, CHATS["drone"], M, CONTENT_TOP, 6.55, 5.2, who="Mary (made up, in English)")
col = 7.55
px, py, pw, ph = picture(s, "upload_page.png", col, CONTENT_TOP, W - M - col, 2.9)
caption(s, "The upload page Mary's link opens.", col, py + ph + 0.08, W - M - col, size=10.5)
text(s, col, 5.05, W - M - col, 1.9,
     [para("However the photos were taken", 19, INK, after=6, bold=True, font=TITLE_FONT),
      para("Flown by hand, by a drone that flies itself, or by a licensed pilot the farmer "
           "hires, the photos arrive the same way. Best on WiFi, and it picks up where it "
           "stopped.", 15, MUTED, spacing=1.1)])
eyes(s, drone=True)
notes(s, "Mary, another made-up farmer, texts DRONE. Two quick questions: was the field bare, "
         "and which day did she fly. Then she gets a link to an upload page. Choose photos, "
         "and they go to the team to be processed. It doesn't matter who flew or how.")

# --------------------------------------------------------------------------- #
# 13  Built for the Valley's sorghum
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "Built for the Valley's sorghum",
      sub="About 410 grain sorghum fields in one search box on the 2025 USDA crop map")
px, py, pw, ph = picture(s, "stage_chart_elsa_en.png", M, 1.75, W - 2 * M, 3.95)
caption(s, "Maria's dryland field near Elsa, 20 May 2025. Heat: gridMET. Stages: Texas A&M "
           "AgriLife B-6137.", px, py + ph + 0.06, pw, h=0.35)
points = [
    ("Stage from heat, not the calendar",
     "Texas A&M's own method: every day's heat, added up since planting.", FIELD),
    ("Water when it decides the harvest",
     "Panicle initiation to flowering sets 70% of the yield.", WATER),
    ("Pests at the right time",
     "Weekly aphid reminders; midge while it flowers.", RUST),
]
gap = 0.35
point_w = (W - 2 * M - 2 * gap) / 3
for i, (heading, body, colour) in enumerate(points):
    icon_row(s, M + i * (point_w + gap), 6.12, point_w, heading, body, colour, size=13, head=15.5)
eyes(s, satellite=True)
notes(s, "Sorghum is everywhere in the Valley, and it has had few tools of its own. Modern "
         "hybrids move with heat, not the calendar, so we add up every day's heat since "
         "planting, the way Texas A&M teaches, and read off the stage. The shaded band is "
         "panicle initiation to flowering, when each head decides how many grains it will "
         "fill: that's when the water warning goes out. And because we know the stage, the "
         "aphid threshold and the midge window are the right ones for that week.")

# --------------------------------------------------------------------------- #
# 12  Eye two: what a drone flight adds
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "What a drone flight adds")
half = (W - 2 * M - 0.5) / 2
px, py, pw, ph = picture(s, "sorghum_flags.png", M, CONTENT_TOP, half, 4.55)
text(s, M, py + ph + 0.12, half, 0.75,
     [para("Plant by plant", 17, INK, after=2, bold=True),
      para("394 of 3,971 stretches of row worth a look. Sorghum trial, Purdue University.",
           12, MUTED)])
px, py, pw, ph = picture(s, "terrain_corn.png", M + half + 0.5, CONTENT_TOP, half, 4.55)
text(s, M + half + 0.5, py + ph + 0.12, half, 0.75,
     [para("The lie of the land", 17, INK, after=2, bold=True),
      para("Low corners where water sits, high spots it never reaches. Government lidar.",
           12, MUTED)])
eyes(s, drone=True)
notes(s, "When a farmer does fly, the drone adds two things the satellite can't see. On the "
         "left, every stretch of row judged against the rest of the field: orange is worth a "
         "walk. On the right, the shape of the ground: blue is low, where water pools; brown "
         "is high, where water may never reach.")

# --------------------------------------------------------------------------- #
# 13  Thermal
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "A thermal camera finds hot spots early")
px, py, pw, ph = picture(s, "terraref_thermal.png", M, CONTENT_TOP + 0.35, 5.5, 2.2,
                         align="left", border=False)
caption(s, "Real sorghum under a real thermal camera: the plants (dark) run about 8 °C cooler "
           "than the soil. Public research data from Arizona, not our field.",
        M, py + ph + 0.08, pw, size=10.5)
text(s, M, py + ph + 0.62, pw, 1.5,
     [para("2,719 pictures in 84 minutes", 17, INK, after=3, bold=True, font=TITLE_FONT),
      para("Our software stitches them into one map of leaf temperature, taking out the sun "
           "climbing while the camera worked. It found three warm patches, the biggest 29 m2.",
           14, MUTED, spacing=1.1)])
col = M + pw + 0.55
tw = W - M - col
text(s, col, CONTENT_TOP, tw, 1.3,
     [para("Healthy plants sweat to stay cool.", 19, INK, after=4, bold=True, font=TITLE_FONT),
      para("A patch running hotter than the rest of the field is a warning, often before "
           "anything shows to the eye.", 15, MUTED, spacing=1.1)])
thermal_line = [dict(CHATS["water"][4],
                     body="Cámara térmica: 50% de probabilidad de plaga o enfermedad en la "
                        "esquina suroeste de Scanner Field (29 m2). Vaya a verlo; la cámara no "
                        "sabe qué es. Hay 2 manchas más; vienen en el archivo de PORQUE.",
                     en="Thermal camera: 50% chance of a pest or disease in the south-west "
                        "corner of Scanner Field (29 m2). Go and look; the camera can't name "
                        "it. There are 2 more patches; they're in the WHY file.")]
chat(s, thermal_line, col, CONTENT_TOP + 1.45, tw, 2.25, who="Ana (made up)", size=13)
icon_row(s, col, 5.4, tw, "Never \"certain\"",
         "Each patch is scored using the ground, the water and what the normal camera sees. "
         "The score stays between 10% and 85%.", RUST, size=14, head=16)
eyes(s, drone=True)
notes(s, "A healthy plant cools itself, like sweating. A plant that stops, from thirst or "
         "because something is eating it, warms up, often before you can see anything. This "
         "is real data: a research field scanner ran a thermal camera over sorghum in Arizona "
         "for 84 minutes and wrote 2,719 pictures, and our software stitches them into one "
         "map of leaf temperature. Two things had to be solved: telling leaves from soil with "
         "no colour camera, and taking out the sun climbing while the camera worked. We score "
         "each warm patch using everything else we know, and we never say certain: the text "
         "always says go and look.")

# --------------------------------------------------------------------------- #
# 14  One page
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "One page, with one eye or two")
left_w, right_w = 5.95, W - 2 * M - 5.95 - 0.55
text(s, M, CONTENT_TOP, left_w, 0.45,
     [([("Both eyes  ", 17, INK, {"bold": True}),
        ("satellite and drone, sorghum", 14, MUTED, {})], {})])
picture(s, "sorghum_page.png", M, CONTENT_TOP + 0.5, left_w, 4.65, align="left")
rx = M + left_w + 0.55
text(s, rx, CONTENT_TOP, right_w, 0.45,
     [([("One eye  ", 17, INK, {"bold": True}),
        ("drone only, a small citrus grove", 14, MUTED, {})], {})])
px, py, pw, ph = picture(s, "citrus_page.png", rx, CONTENT_TOP + 0.5, right_w, 3.9, align="left")
caption(s, "Too small for the satellite to see. The page says so plainly instead of leaving a "
           "gap.", rx, py + ph + 0.12, right_w, h=0.7, size=12)
eyes(s, satellite=True, drone=True)
notes(s, "All of it lands on one page a farmer can save or forward. On the left, a field with "
         "both eyes: what the satellite says first, then the flight. On the right, a grove too "
         "small for the satellite. The page doesn't hide that, it says it, and shows what the "
         "drone found on its own.")

# --------------------------------------------------------------------------- #
# 15  Who flies
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "Who flies the drone? Nobody has to.")
text(s, M, CONTENT_TOP + 0.1, 4.6, 1.4, [para("$175", 88, RUST, bold=True, font=TITLE_FONT)])
text(s, M, CONTENT_TOP + 1.65, 4.3, 2.4,
     [para("The FAA Part 107 exam to fly a drone for your farm. Not thousands.", 18, INK,
           after=10, bold=True, spacing=1.1),
      para("The expensive licence is Part 137, for spraying. Dos Ojos never sprays.", 15,
           MUTED, spacing=1.1)])
col = 5.75
icon_row(s, col, CONTENT_TOP + 0.1, W - M - col, "Satellite only: nobody flies",
         "Where everyone starts, and where most farmers will stay.", FIELD, size=15)
icon_row(s, col, CONTENT_TOP + 1.6, W - M - col, "Fly your own",
         "By hand, or with a drone that flies its own route. Either way, it needs the licence.",
         RUST, size=15)
icon_row(s, col, CONTENT_TOP + 3.1, W - M - col, "One pilot, many farms",
         "A co-op, ag retailer, crop consultant or college drone students. The satellite "
         "tells them which few fields need a visit this week.", WATER, size=15)
eyes(s, satellite=True, drone=True)
notes(s, "The question we always get: do farmers need a licence? Only to fly, and it's a $175 "
         "exam, not thousands. The expensive one is for crop spraying, which we don't do. But "
         "nobody has to fly at all: satellite only is the default. And when a flight is worth "
         "it, one licensed pilot can serve many farms, sent by the satellite to the few fields "
         "that need it.")

# --------------------------------------------------------------------------- #
# 16  Honest about accuracy
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "Checked against the tape measure")
px, py, pw, ph = picture(s, "citrus_answer_key.png", M, CONTENT_TOP, 5.0, 4.8, align="left")
caption(s, "USDA-ARS citrus trial, Fort Pierce, Florida. Public data.", M, py + ph + 0.1, pw,
        size=10.5)
col = M + pw + 0.6
text(s, col, CONTENT_TOP, W - M - col, 1.5,
     [([("206 trees", 44, INK, {"bold": True, "font": TITLE_FONT})], {"after": 2}),
      para("measured by hand by USDA scientists, then flown with a drone and scored.", 16,
           MUTED, spacing=1.1)])
icon_row(s, col, CONTENT_TOP + 1.75, W - M - col, "It mostly gets the order right",
         "Taller trees read taller. Good for finding the weakest trees in a grove.", FIELD,
         size=15)
icon_row(s, col, CONTENT_TOP + 3.0, W - M - col, "It reads heights about 21% short",
         "Better processing already cut that from 40%. We found it because we checked.", RUST,
         size=15)
icon_row(s, col, CONTENT_TOP + 4.25, W - M - col, "So we say what it's good for",
         "Comparing trees with each other, not measuring them to the inch.", WATER, size=15)
eyes(s, drone=True)
notes(s, "Most demos stop at a nice picture. We found a dataset where every tree had been "
         "measured by hand, and scored ourselves. Each dot is one tree: across is the real "
         "height, up is what the drone measured. The order is mostly right, but everything reads "
         "short. Better processing halved that error. We tell farmers what the numbers are good "
         "for, and what they're not.")

# --------------------------------------------------------------------------- #
# 17  What's next
# --------------------------------------------------------------------------- #

s = new_slide()
title(s, "What's next")
text(s, M, 1.7, W - 2 * M, 1.6,
     [para("All of it runs today: the satellite, the water account, sorghum stages and pests, "
           "a real drone flight and a real thermal scan, and the text messages around them "
           "— on free public data, because we have flown nothing ourselves yet.", 17,
           MUTED, spacing=1.15),
      para("What is left is what it takes to reach farms.", 19, INK, bold=True,
           font=TITLE_FONT)])
nexts = [
    ("A camera in the Valley", "The thermal step works on real research data from Arizona. "
                               "Next: a thermal flight over a Valley field.", RUST),
    ("A real phone number", "Connect the text messages to a live number.", WATER),
    ("First Valley farms", "Try it with growers, in Spanish and English.", FIELD),
    ("Routes for pilots", "Hand a pilot a ready-made flight plan for each field.", RUST),
]
gap = 0.35
item_w = (W - 2 * M - 3 * gap) / 4
for i, (heading, body, colour) in enumerate(nexts):
    icon_row(s, M + i * (item_w + gap), 5.3, item_w, heading, body, colour, size=13.5, head=16)
eyes(s, satellite=True, drone=True)
notes(s, "What's next. The pest warning now runs on real thermal data, but that data is from "
         "a research farm in Arizona: the next step is a thermal camera over a Valley field. "
         "After that: a live phone number, the first farms in the Valley, and ready-made "
         "flight routes for pilots.")

# --------------------------------------------------------------------------- #
# 18  Close
# --------------------------------------------------------------------------- #

s = new_slide(dark=True)
shape(s, MSO_SHAPE.OVAL, M, 1.35, 1.0, 1.0, fill=FIELD)
shape(s, MSO_SHAPE.OVAL, M + 1.18, 1.35, 1.0, 1.0, fill=RUST)
text(s, M, 2.7, 11.5, 1.2, [para("Dos Ojos", 64, ON_DARK, bold=True, font=TITLE_FONT)])
text(s, M, 3.85, 11.5, 0.7, [para("Two eyes on every field.", 28, ON_DARK_MUTED,
                                  font=TITLE_FONT)])
text(s, M, 4.85, 11.5, 0.7, [para("Questions?", 26, ON_DARK, bold=True)])
text(s, M, 6.35, W - 2 * M, 0.7,
     [para("Public data used: Sentinel-2 (Copernicus), USDA soil survey, gridMET weather, "
           "USGS 3DEP lidar, Purdue University, USDA-ARS Fort Pierce, TERRA-REF. The farmers in "
           "the demo are made up.", 11, ON_DARK_MUTED, spacing=1.05)])
notes(s, "Thank the audience and take questions. The sources line is there so nobody mistakes "
         "public research data for our own farms.")

prs.save(OUT)
print(f"wrote {OUT} ({SLIDE_NO} slides)")
