"""Ergebnisse der Handauswertung (data/handauswertung/arbeit/ergebnis_*.json) prüfen und übernehmen.

Prüft je relevantem Vorhaben, ob `evidenz` wörtlich im Auszug steht (Leerraum normalisiert), und schreibt die
bestandenen Einträge nach data/handauswertung/projekte.json - von dort liest sie radar.demo_data in die Demo-/
Test-Datenbank (nie data/radar.db). Nicht bestandene Einträge werden nur gemeldet.

Aufruf: .venv\\Scripts\\python.exe scripts\\handauswertung_uebernehmen.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar.parser import ProjektExtraktion  # noqa: E402

ARBEIT = ROOT / "data" / "handauswertung" / "arbeit"
ZIEL = ROOT / "data" / "handauswertung" / "projekte.json"
RATSINFO = "https://buergerinfo.norderstedt.de/ratsinfo/sessionnet/buergerinfo/"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _quote(wert) -> float | None:
    if wert in (None, ""):
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(wert))
    if not m:
        return None
    zahl = float(m.group(1).replace(",", "."))
    return zahl / 100 if zahl > 1 else zahl


# Von Hand geprüfte Korrekturen je Auszugsdatei (überschreiben Felder des Helfer-Ergebnisses).
KORREKTUREN: dict[str, dict] = {
    # B-Plan 341 Bereich 2 ist Instones "Nordlicht" (Kösliner Weg 7-9): laut Presse 198 Eigentumswohnungen,
    # Grundstein gelegt (Web-Fund, verifiziert 27.10.2026). Die 30 % Förderquote gilt für das ganze Plangebiet,
    # nicht für diesen freifinanzierten Bereich.
    "05.txt": dict(
        projektbezeichnung="Quartier Kösliner Weg = \"Nordlicht\" (Instone, B-Plan 341)",
        antragsteller="Instone Real Estate Development GmbH",
        wohnform="EIGENTUM",
        quote_gefoerdert=None,
        pruefen="Dasselbe Projekt wie der Pressefund \"Nordlicht\" (198 Eigentumswohnungen, Grundstein gelegt) - "
        "nicht doppelt zählen.",
    ),
}


def eintrag(e: dict) -> dict:
    e = {**e, **KORREKTUREN.get(e.get("datei"), {})}
    gefoerdert = e.get("we_gefoerdert_anzahl")
    quote = _quote(e.get("quote_gefoerdert"))
    extraktion = dict(
        projektbezeichnung=e.get("projektbezeichnung"),
        adresse=dict(strasse=e.get("strasse"), ort=e.get("ort") or "Norderstedt"),
        we_gesamt=e.get("we_gesamt"),
        we_miete=e.get("we_miete"),
        we_gefoerdert_anzahl=gefoerdert,
        we_gefoerdert=True if (gefoerdert or quote) else None,
        quote_gefoerdert=quote,
        ausfuehrungszeitraum=e.get("ausfuehrungszeitraum"),
        antragsteller=e.get("antragsteller"),
        kurzfassung=f"{e.get('kurzfassung') or ''} Vorlage {e.get('vorlagen_nr')}. (Handauswertung 24.11.2026)".strip(),
        wohnform=e.get("wohnform") or "UNKLAR",
        verfahrensstand=e.get("verfahrensstand") or "UNKLAR",
        konfidenz=e.get("konfidenz") or 0.0,
        evidenz=(e.get("evidenz") or None) and e["evidenz"][:200],
        seite=e.get("seite"),
    )
    ProjektExtraktion(**extraktion)
    notiz = "Handauswertung 24.11.2026, Ratsinfo " + str(e.get("vorlagen_nr"))
    if e.get("pruefen"):
        notiz += ". Prüfen: " + e["pruefen"]
    url = e.get("quelle_url") or ""
    if url and not url.startswith("http"):  # Ratsinfo speichert getfile.php-Links relativ
        url = RATSINFO + url.lstrip("/")
    return dict(extraktion=extraktion, quelle_url=url or None, notiz=notiz)


def main() -> int:
    ok, fehler, nicht_relevant = [], [], []
    for datei in sorted(ARBEIT.glob("ergebnis_*.json")):
        for e in json.loads(datei.read_text(encoding="utf-8")):
            if e.get("datei") == "22.txt" or not e.get("relevant"):
                nicht_relevant.append(e)
                continue
            auszug = _norm((ARBEIT / "auszuege" / e["datei"]).read_text(encoding="utf-8"))
            ev = _norm(e.get("evidenz") or "")
            if ev and ev in auszug:
                ok.append(eintrag(e))
            else:
                fehler.append(e)
    ZIEL.write_text(json.dumps(ok, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"übernommen: {len(ok)}, Zitat nicht gefunden: {len(fehler)}, "
          f"nicht relevant/Querabgleich: {len(nicht_relevant)}")
    for e in fehler:
        print("  ZITAT FEHLT:", e.get("datei"), e.get("vorhaben"), "|", (e.get("evidenz") or "")[:90])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
