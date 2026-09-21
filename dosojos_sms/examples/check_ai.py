"""Try the local AI for real: farmers' own words, then a recommendation per field.

Needs Ollama running with the model pulled (ollama pull llama3.2:3b). Nothing is
texted and nothing is stored except the AI's kept advice, which the WHY pages and
texts then reuse. From any folder:

    dosojos_sat\\.venv\\Scripts\\python.exe dosojos_sms\\examples\\check_ai.py --data live_data
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dosojos_sms import ai, store
from dosojos_sms.bot import ai_text
from dosojos_sms.config import Settings
from dosojos_sms.status import Water

MESSAGES = [
    ("es", "anoche cayo como una pulgada de agua"),
    ("es", "le dimos una buena regada al citrico antier como 2 pulgadas"),
    ("es", "oiga y cuanto le falta al sorgo pa regarlo"),
    ("en", "we just finished cutting the sorghum today"),
    ("en", "can you show me the charts for the grove"),
    ("es", "gracias compa"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, required=True, help="e.g. live_data")
    parser.add_argument("--phone", default=None, help="one farmer (default: the first)")
    args = parser.parse_args()
    settings = Settings.load(args.data.resolve())
    model = ai.ready(settings)
    if model is None:
        print(f"No model at {settings.ai_url} with {settings.ai_model}. Is Ollama running, "
              f"and did 'ollama pull {settings.ai_model}' finish?")
        return 1
    water, today = Water(settings), (settings.as_of or settings.now().date())
    with store.session(settings.db_path) as conn:
        farmers = [f for f in store.farmers(conn) if not args.phone or f.phone == args.phone]
        farmer = next((f for f in farmers if store.fields_of(conn, f.phone)), None)
        if farmer is None:
            print("No farmer with fields in that folder.")
            return 1
        records = store.fields_of(conn, farmer.phone)
        events = {r.id: store.events_for(conn, r.id) for r in records}
    fields = []
    for r in records:
        s = water.field(r, events[r.id], today).status
        fields.append({"name": r.name, "summary": (f"{r.crop}, water in {s.days_left} days"
                                                   if s and s.days_left is not None else r.crop)})
    print(f"Model {settings.ai_model} on {settings.ai_url}; farmer {farmer.name}, "
          f"{len(records)} field(s)\n\nREADING FARMERS' OWN WORDS")
    for lang, message in MESSAGES:
        started = time.monotonic()
        try:
            heard = ai.understand(model, message, lang=lang, today=today, fields=fields)
        except ai.AIError as exc:
            print(f"  {message!r}\n    FAILED: {exc}")
            continue
        said = heard.answer or heard.command_text(message) or "(passed to the team)"
        print(f"  {message!r}\n    -> {heard.intent}: {said}   ({time.monotonic() - started:.0f} s)")

    print("\nTHE RECOMMENDATION, PER FIELD")
    for r in records:
        item = water.field(r, events[r.id], today, full=True)
        brief = ai.field_brief(settings, farmer, item, events[r.id], today)
        if brief is None:
            print(f"  {r.name}: no checkbook yet ({item.reason})")
            continue
        started = time.monotonic()
        advice = ai.recommend(model, brief, lang=farmer.language,
                              now=settings.now().isoformat(timespec="minutes"))
        took = time.monotonic() - started
        if advice is None:
            print(f"  {r.name}: refused twice by the checks; the checkbook answer goes out "
                  f"({took:.0f} s)")
            continue
        ai.keep(settings, r.id, ai.brief_key(brief, settings.ai_model), advice, brief)
        print(f"  {ai_text(r.name, advice, farmer.language)}   ({took:.0f} s)")
        for reason in advice.reasons:
            print(f"      - {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
