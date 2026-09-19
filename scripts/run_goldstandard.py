"""Goldstandard-Auswertung für die LLM-Extraktion (Schritt 3), siehe CLAUDE.md "Qualitätssicherung".

Macht ECHTE, kostenpflichtige Aufrufe an die Anthropic-API (ANTHROPIC_API_KEY nötig) - deshalb
bewusst kein Teil von `pytest` (siehe CLAUDE.md: "Tests ohne Netz"). Manuell ausführen, vor allem
nach jeder Änderung an den Prompts unter config/prompts/:

    .venv\\Scripts\\python.exe scripts\\run_goldstandard.py

Vergleicht `tests/gold/faelle.yaml` (synthetische Testfälle, keine echten Sitzungsunterlagen)
gegen die tatsächliche Pipeline-Ausgabe (Stufe 1 Keyword-Vorfilter, Stufe 2 Triage, Stufe 3
Extraktion) und druckt pro Fall Treffer/Fehlschlag sowie eine Gesamttrefferquote.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anthropic
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.config import load_settings  # noqa: E402
from radar.parser import TriageEntscheidung, build_excerpt, extract, matched_keywords, triage  # noqa: E402

GOLD_FILE = Path(__file__).resolve().parent.parent / "tests" / "gold" / "faelle.yaml"


def _check_field(erwartet: dict, feld: str, wert, tol: float = 0.0) -> str | None:
    if feld not in erwartet:
        return None
    soll = erwartet[feld]
    if isinstance(soll, float):
        if wert is None or abs(float(wert) - soll) > tol + 1e-9:
            return f"{feld}: erwartet {soll}, erhalten {wert}"
        return None
    if wert != soll:
        return f"{feld}: erwartet {soll!r}, erhalten {wert!r}"
    return None


def run_case(client: anthropic.Anthropic, settings, case: dict) -> tuple[bool, str]:
    seiten: list[str] = case["seiten"]
    erwartet: dict = case["erwartet"]
    soll_relevant = erwartet.get("relevant", True)
    keywords = settings.filters.prefilter_keywords

    if not matched_keywords("\n".join(seiten), keywords):
        ok = not soll_relevant
        return ok, "Stufe 1 (Keyword-Vorfilter): kein Treffer" + ("" if ok else " – erwartet aber relevant")

    excerpt = build_excerpt(seiten, keywords, settings.llm.context_pages, settings.llm.max_input_chars)

    triage_ergebnis = triage(client, settings, excerpt, quelle_id="goldstandard")
    if triage_ergebnis.entscheidung is TriageEntscheidung.NEIN:
        ok = not soll_relevant
        grund = f"Triage NEIN ({triage_ergebnis.begruendung})"
        return ok, grund + ("" if ok else " – erwartet aber relevant")

    if not soll_relevant:
        return False, f"Triage {triage_ergebnis.entscheidung.value} – erwartet NICHT relevant"

    extraktion = extract(client, settings, excerpt, quelle_id="goldstandard")
    if extraktion is None:
        return False, "Extraktion nach Reparaturversuchen ungültig"

    fehler = [
        m
        for m in (
            _check_field(erwartet, "wohnform", extraktion.wohnform.value),
            _check_field(erwartet, "verfahrensstand", extraktion.verfahrensstand.value),
            _check_field(erwartet, "we_gesamt", extraktion.we_gesamt),
            _check_field(erwartet, "we_miete", extraktion.we_miete),
            _check_field(erwartet, "we_gefoerdert_anzahl", extraktion.we_gefoerdert_anzahl),
            _check_field(erwartet, "unklar", extraktion.unklar),
            _check_field(erwartet, "quote_gefoerdert", extraktion.quote_gefoerdert, tol=0.05),
        )
        if m is not None
    ]
    return (not fehler), "; ".join(fehler) if fehler else "OK"


def main() -> None:
    settings = load_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)  # wirft früh, falls Key fehlt
    faelle = yaml.safe_load(GOLD_FILE.read_text(encoding="utf-8"))["faelle"]

    treffer = 0
    for case in faelle:
        ok, detail = run_case(client, settings, case)
        treffer += int(ok)
        print(f"[{'OK  ' if ok else 'FEHL'}] {case['id']} ({case['bundesland']}): {detail}")

    print(f"\n{treffer}/{len(faelle)} Fälle bestanden ({treffer / len(faelle):.0%}).")


if __name__ == "__main__":
    main()
