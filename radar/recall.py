"""Recall gegen eine Liste bekannter Projekte (Hummel-Abgleich 11/2026).

Hummel Küchen kannte 11 Norderstedter Projekte, die das Radar nicht zeigte - aufgefallen ist das nur durch
einen manuellen Abgleich. Dieses Modul misst das automatisch: wie viele der bekannten Projekte hat das
Radar **selbst** gefunden (`python -m radar recall`, Test `tests/test_recall.py`)?

Gezählt werden nur automatisch gefundene Einträge: Projekte aus Ratsunterlagen mit `herkunft = 'radar'`
(solange die Spalte fehlt: alles außer den manuell geprüften Echtprojekten `demo_data.ECHTE_QUELLE_ID`)
und Web-Recherche-Funde. Ein bekanntes Projekt gilt als gefunden, wenn einer seiner Suchbegriffe in
Bezeichnung, Adresse oder Kurzfassung steht (bzw. Titel/Auszug eines Web-Funds). Reine Zahlen sind
B-Plan-Nummern ("322" trifft "Bebauungsplan Nr. 322", nicht "193 Wohneinheiten").
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from radar.config import PROJECT_ROOT

STANDARD_LISTE = PROJECT_ROOT / "tests" / "fixtures" / "known_projects_norderstedt.json"
MANUELLE_QUELLEN = {"echte_projekte"}  # = demo_data.ECHTE_QUELLE_ID, ohne den schweren Import
LETZTER_BERICHT: str | None = None  # von `pytest -m recall` gesetzt, am Ende ausgegeben (tests/conftest.py)


@dataclass(frozen=True)
class BekanntesProjekt:
    name: str
    suche: tuple[str, ...]
    hummel_nr: int | None = None
    nur_presse: bool = False
    ratsvorlage: str | None = None  # von uns geprüfte Vorlage mit WE-Zahl (z. B. "B 24/0295")


@dataclass
class RecallErgebnis:
    gefunden: list[tuple[BekanntesProjekt, str]] = field(default_factory=list)  # (Projekt, Fundstelle)
    fehlend: list[BekanntesProjekt] = field(default_factory=list)

    @property
    def anzahl(self) -> int:
        return len(self.gefunden) + len(self.fehlend)

    def zusammenfassung(self) -> str:
        return f"Das Radar findet {len(self.gefunden)} von {self.anzahl} bekannten Projekten"


def lade_bekannte_projekte(pfad: Path = STANDARD_LISTE) -> list[BekanntesProjekt]:
    daten = json.loads(Path(pfad).read_text(encoding="utf-8"))
    return [
        BekanntesProjekt(
            name=p["name"],
            suche=tuple(p["suche"]),
            hummel_nr=p.get("hummel_nr"),
            nur_presse=bool(p.get("nur_presse")),
            ratsvorlage=p.get("ratsvorlage"),
        )
        for p in daten["projekte"]
    ]


def normalisiere(text: str) -> str:
    """Kleinbuchstaben, "Straße"/"Strasse"/"Str." -> "str", einheitliche Bindestriche und Leerzeichen."""
    text = text.lower().replace("ß", "ss")
    text = re.sub(r"strasse|str\.", "str", text)
    text = re.sub(r"[‐-―]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _muster(begriff: str) -> re.Pattern:
    begriff = normalisiere(begriff)
    if begriff.isdigit():
        return re.compile(rf"(?:bebauungsplan(?:es)?|b-plan|bplan)\s*(?:nr\s*\.?\s*)?{begriff}(?!\d)")
    # Ein Begriff, der auf eine Ziffer endet ("Berliner Allee 4"), darf nicht "Berliner Allee 45" treffen.
    ende = r"(?!\d)" if begriff[-1].isdigit() else ""
    return re.compile(re.escape(begriff) + ende)


def _trifft(bekannt: BekanntesProjekt, text: str) -> str | None:
    norm = normalisiere(text)
    for begriff in bekannt.suche:
        if _muster(begriff).search(norm):
            return begriff
    return None


def _ist_radar_projekt(row: sqlite3.Row) -> bool:
    # Quelle echte_projekte ist immer von Hand eingetragen - auch wenn ein neuer Eintrag dort (noch) mit der
    # Standard-Herkunft 'radar' angelegt wurde.
    if row["quelle_id"] in MANUELLE_QUELLEN:
        return False
    return "herkunft" not in row.keys() or row["herkunft"] == "radar"


def _texte(conn: sqlite3.Connection, kommune: str) -> list[tuple[str, str]]:
    """(Beschreibung der Fundstelle, durchsuchbarer Text) aller automatisch gefundenen Einträge."""
    texte: list[tuple[str, str]] = []
    for row in conn.execute("SELECT * FROM projekte"):
        if not _ist_radar_projekt(row):
            continue
        if kommune.lower() not in f"{row['kommune'] or ''} {row['ort'] or ''}".lower():
            continue
        adresse = " ".join(filter(None, [row["strasse"], row["hausnummer"], row["flurstueck"]]))
        text = " | ".join(filter(None, [row["projektbezeichnung"], adresse, row["kurzfassung"]]))
        texte.append((f"Projekt {row['id']}: {row['projektbezeichnung'] or adresse or '(ohne Bezeichnung)'}", text))
    tabellen = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "web_funde" in tabellen:
        for row in conn.execute("SELECT id, titel, auszug, ort FROM web_funde"):
            if kommune.lower() not in (row["ort"] or "").lower():
                continue
            texte.append((f"Web-Fund {row['id']}: {row['titel']}", f"{row['titel']} | {row['auszug'] or ''}"))
    return texte


def messe_recall(
    conn: sqlite3.Connection, bekannte: list[BekanntesProjekt], *, kommune: str = "Norderstedt"
) -> RecallErgebnis:
    conn.row_factory = sqlite3.Row
    texte = _texte(conn, kommune)
    ergebnis = RecallErgebnis()
    for bekannt in bekannte:
        fund = next(
            (
                f"{beschreibung} (Begriff „{begriff}“)"
                for beschreibung, text in texte
                if (begriff := _trifft(bekannt, text))
            ),
            None,
        )
        if fund:
            ergebnis.gefunden.append((bekannt, fund))
        else:
            ergebnis.fehlend.append(bekannt)
    return ergebnis


def messe_unterlagen(conn: sqlite3.Connection, bekannte: list[BekanntesProjekt]) -> dict[str, str]:
    """Zweite Zahl neben dem Recall: Für welche bekannten Projekte liegt die von uns geprüfte Ratsvorlage
    (`ratsvorlage` in der Liste, mit WE-Zahl belegt) samt heruntergeladenem PDF bereits in der Datenbank? Zeigt,
    was das Radar mit eingeschalteter KI-Auswertung finden *könnte* - kein Fund des Radars. Bewusst über geprüfte
    Vorlagen-Nr. statt über eine Volltextsuche: die zählte am 19.11.2026 bloße Erwähnungen (Straßennamen auf
    Karten, Sammelvorlagen) mit und kam auf 17 von 18, darunter nachweislich nur über die Presse bekannte
    Projekte. Ergebnis: Projektname -> Fundstelle ("Vorlage B 24/0295 (2024-09-05, Ausschuss ...)")."""
    conn.row_factory = sqlite3.Row
    funde: dict[str, str] = {}
    for bekannt in bekannte:
        if not bekannt.ratsvorlage:
            continue
        zeile = conn.execute(
            """SELECT s.datum, s.gremium FROM vorlagen v JOIN sitzungen s ON s.id = v.sitzung_id
               WHERE v.vorlagen_nr = ? AND EXISTS (
                   SELECT 1 FROM dokumente d WHERE d.vorlage_id = v.id AND d.dateipfad IS NOT NULL)
               ORDER BY s.datum LIMIT 1""",
            (bekannt.ratsvorlage,),
        ).fetchone()
        if zeile:
            funde[bekannt.name] = f"Vorlage {bekannt.ratsvorlage} ({zeile['datum']}, {zeile['gremium']})"
    return funde


def bericht(ergebnis: RecallErgebnis, unterlagen: dict[str, str] | None = None) -> str:
    zeilen = [ergebnis.zusammenfassung() + "."]
    if ergebnis.gefunden:
        zeilen.append("\nGefunden:")
        # Schlichte Zeichen statt Häkchen: die Windows-Konsole (cp1252) kann ✓/✗ nicht ausgeben.
        zeilen += [f"  [x] {_label(p)} -> {fund}" for p, fund in ergebnis.gefunden]
    if ergebnis.fehlend:
        zeilen.append("\nFehlt:")
        presse = " (nur über Presse auffindbar)"
        zeilen += [f"  [ ] {_label(p)}{presse if p.nur_presse else ''}" for p in ergebnis.fehlend]
    if unterlagen is not None:
        zeilen.append(
            f"\nRatsunterlagen bereits geladen: {len(unterlagen)} von {ergebnis.anzahl} "
            "(geprüfte Vorlage mit WE-Zahl liegt vor - mit KI-Auswertung auffindbar, noch kein Fund des Radars):"
        )
        zeilen += [f"  - {name}: {fund}" for name, fund in unterlagen.items()]
    return "\n".join(zeilen)


def _label(p: BekanntesProjekt) -> str:
    return f"{p.name} [Hummel {p.hummel_nr}]" if p.hummel_nr else p.name
