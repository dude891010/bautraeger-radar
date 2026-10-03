"""Demo-/Beispieldaten für das Dashboard (Schritt 5, nachträglich ergänzt).

Zweck: dem Vertrieb (und für Präsentationen) ein sichtbar volles Dashboard zeigen, ohne auf einen
echten Scraper-Lauf oder Anthropic-Guthaben zu warten (siehe CLAUDE.md Schritt 3: Live-Aufrufe
scheitern aktuell an `Your credit balance is too low`). Schreibt dafür **niemals** in die echte
`data/radar.db`, sondern in eine eigene Datei `data/demo/demo.db` - über dieselbe Datenbankschicht
(`radar.database`) wie ein echter Lauf, damit Schema und Dashboard-Abfragen garantiert
zusammenpassen und nicht separat gepflegt werden müssen.

`seed()` läuft über `database.upsert_sitzung()`/`upsert_vorlage()`/`upsert_projekt()` - genau wie
ein echter Lauf - und ist deshalb wie dieser idempotent (dieselben `external_id`s/Dedup-Schlüssel
bei jedem Aufruf): beliebig oft wiederholbar, ohne Duplikate. `app.py` ruft `ensure_demo_seeded()`
nur beim *ersten* Aktivieren des Demo-Schalters auf (leere Demo-DB) - Status-/Notizen-Änderungen,
die im Demo-Modus über den Dashboard-Editor gemacht werden, bleiben danach erhalten, bis
`scripts/seed_demo_data.py` die Demo-Werte manuell zurücksetzt.

9 synthetische Projekte, frei erfunden, über alle sechs Zielregionen verteilt (CLAUDE.md
"Zielgebiet"), 12-85 Wohneinheiten, unterschiedliche Verfahrensstände/Status - inklusive eines
bewusst unklaren Falls (fehlende Förderzahl, `wohnform=UNKLAR`), der CLAUDE.mds Kernregel
"unklare Fälle nie stillschweigend verwerfen" im Dashboard sichtbar macht.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

from radar import database
from radar.config import DatabaseConfig, Settings
from radar.parser import Adresse, ProjektExtraktion, Verfahrensstand, Wohnform
from radar.sources.base import Sitzung

# Tier kommt im echten Betrieb aus config/sources.yaml (radar.config.Settings.tier_for). Die
# Demo-Quellen unten sind keine echten Ratsinformationssysteme und stehen dort bewusst nicht -
# app.py mischt diese Zuordnung bei aktivem Demo-Modus zusätzlich in `tier_by_quelle` ein.
DEMO_TIER_BY_QUELLE: dict[str, str] = {
    "norderstedt": "A",
    "bv_hh_bergedorf": "A",
    "hamburg_transparenz": "A",
    "uplengen": "B",
    "demo_bremen_vegesack": "B",
    "demo_rostock": "B",
    "demo_berlin_pankow": "C",
    "demo_bad_oldesloe": "A",
}

# (id, name, bundesland, system) - system nur zur Anzeige im Quellen-Tab, keine echte Adapterwahl.
_QUELLEN: list[tuple[str, str, str, str]] = [
    ("norderstedt", "Stadt Norderstedt", "SH", "sessionnet"),
    ("bv_hh_bergedorf", "bv-hh.de (Bezirksversammlung Bergedorf)", "HH", "bv_hh"),
    ("hamburg_transparenz", "Transparenzportal Hamburg (Bauleitpläne)", "HH", "hamburg_transparenz"),
    ("uplengen", "Gemeinde Uplengen", "NI", "oparl"),
    ("demo_bremen_vegesack", "Ortsamt Bremen-Vegesack (Demo)", "HB", "unknown"),
    ("demo_rostock", "Hansestadt Rostock (Demo)", "MV", "unknown"),
    ("demo_berlin_pankow", "Bezirksamt Berlin-Pankow (Demo)", "BE", "unknown"),
    ("demo_bad_oldesloe", "Stadt Bad Oldesloe (Demo)", "SH", "unknown"),
]

_PROJEKTE: list[dict] = [
    dict(
        quelle_id="norderstedt",
        bundesland="SH",
        kommune="Norderstedt",
        gremium="Ausschuss für Stadtentwicklung und Verkehr",
        sitzungsdatum=date(2026, 8, 18),
        sitzung_url="https://buergerinfo.norderstedt.de/ratsinfo/sessionnet/buergerinfo/si0057.php?__ksinr=8841",
        vorlagen_nr="A 26/0338",
        projektbezeichnung="Wohnquartier Ochsenzoll",
        strasse="Ochsenzoller Straße",
        hausnummer="145",
        flurstueck="234/12",
        ort="Norderstedt",
        we_gesamt=42,
        we_miete=42,
        we_gefoerdert_anzahl=12,
        we_gefoerdert=True,
        quote_gefoerdert=0.29,
        ausfuehrungszeitraum="Baubeginn voraussichtlich Frühjahr 2028",
        antragsteller="Wohnungsbaugenossenschaft Norderstedt eG",
        kurzfassung=(
            "Neubau von vier Mehrfamilienhäusern mit 42 Mietwohnungen, davon 12 öffentlich "
            "gefördert, auf dem ehemaligen Gärtnereigelände an der Ochsenzoller Straße."
        ),
        wohnform=Wohnform.GEMISCHT,
        verfahrensstand=Verfahrensstand.AUFSTELLUNGSBESCHLUSS,
        konfidenz=0.86,
        evidenz="Es entstehen 42 Mietwohneinheiten, davon 12 im 1. Förderweg (Belegungsbindung 25 Jahre).",
        seite=3,
        status="Neu",
    ),
    dict(
        quelle_id="bv_hh_bergedorf",
        bundesland="HH",
        kommune="Hamburg-Bergedorf",
        gremium="Stadtentwicklungsausschuss",
        sitzungsdatum=date(2026, 9, 2),
        sitzung_url="https://bv-hh.de/bergedorf/meetings/2026-09-02-stadtentwicklungsausschuss",
        vorlagen_nr="2025/0187",
        projektbezeichnung="Quartier Vierlanden",
        strasse="Curslacker Neuer Deich",
        hausnummer="88",
        flurstueck=None,
        ort="Hamburg-Bergedorf",
        we_gesamt=68,
        we_miete=68,
        we_gefoerdert_anzahl=22,
        we_gefoerdert=True,
        quote_gefoerdert=0.32,
        ausfuehrungszeitraum="2027-2030 (drei Bauabschnitte)",
        antragsteller="Vierlanden Wohnbau GmbH",
        kurzfassung=(
            "Drittelmix-Quartier mit 68 Wohneinheiten in drei Bauabschnitten; Auslegung des "
            "Bebauungsplanentwurfs beschlossen."
        ),
        wohnform=Wohnform.GEMISCHT,
        verfahrensstand=Verfahrensstand.AUSLEGUNG,
        konfidenz=0.74,
        evidenz="Von den 68 geplanten Wohnungen werden 22 im geförderten Segment (1./2. Förderweg) errichtet.",
        seite=5,
        status="In Prüfung",
    ),
    dict(
        quelle_id="hamburg_transparenz",
        bundesland="HH",
        kommune="Hamburg-Altona",
        gremium="Bauleitplanung",
        sitzungsdatum=date(2026, 7, 14),
        sitzung_url="https://suche.transparenz.hamburg.de/dataset/bebauungsplan-altona-150",
        vorlagen_nr="B-Plan Altona 150",
        projektbezeichnung="Bauvorhaben Holstenkamp",
        strasse="Holstenkamp",
        hausnummer="22",
        flurstueck="1847",
        ort="Hamburg-Altona",
        we_gesamt=85,
        we_miete=55,
        we_gefoerdert_anzahl=30,
        we_gefoerdert=True,
        quote_gefoerdert=0.35,
        ausfuehrungszeitraum="Satzungsbeschluss 07/2026, Baubeginn 2027",
        antragsteller="IBA Altona Projektentwicklung GmbH",
        kurzfassung=(
            "Rechtskräftiger Bebauungsplan für 85 Wohneinheiten im Drittelmix, davon 30 "
            "öffentlich gefördert."
        ),
        wohnform=Wohnform.GEMISCHT,
        verfahrensstand=Verfahrensstand.SATZUNGSBESCHLUSS,
        konfidenz=0.91,
        evidenz="Der Bebauungsplan setzt 85 Wohneinheiten fest, davon mindestens 30 geförderte Wohnungen.",
        seite=12,
        status="Beschlossen",
    ),
    dict(
        quelle_id="uplengen",
        bundesland="NI",
        kommune="Uplengen",
        gremium="Bau- und Planungsausschuss",
        sitzungsdatum=date(2026, 6, 10),
        sitzung_url="https://uplengen.ratsinfomanagement.net/webservice/oparl/v1.1/meeting/4521",
        vorlagen_nr="126/2025",
        projektbezeichnung="Neubaugebiet Remels-West",
        strasse="Hauptstraße",
        hausnummer="5",
        flurstueck="78/3",
        ort="Uplengen-Remels",
        we_gesamt=18,
        we_miete=18,
        we_gefoerdert_anzahl=6,
        we_gefoerdert=True,
        quote_gefoerdert=0.33,
        ausfuehrungszeitraum="Vorberatung, Zeitplan noch offen",
        antragsteller="Gemeinde Uplengen (Konzeptvergabe)",
        kurzfassung=(
            "Konzeptvergabe für ein kleines Neubaugebiet mit 18 Mietwohnungen, davon 6 "
            "gefördert; erste Lesung im Ausschuss."
        ),
        wohnform=Wohnform.GEMISCHT,
        verfahrensstand=Verfahrensstand.VORBERATUNG,
        konfidenz=0.68,
        evidenz="Die Konzeptvergabe sieht 18 Wohneinheiten vor, mindestens 6 davon gefördert.",
        seite=2,
        status="Neu",
    ),
    dict(
        quelle_id="demo_bremen_vegesack",
        bundesland="HB",
        kommune="Bremen-Vegesack",
        gremium="Ortsausschuss Vegesack",
        sitzungsdatum=date(2026, 8, 27),
        sitzung_url="https://www.bremen.de/ortsamt-vegesack/sitzungen/demo-2026-08-27",
        vorlagen_nr="VO/25/0456",
        projektbezeichnung="Wohnpark Vegesack",
        strasse="Rohrstraße",
        hausnummer="12",
        flurstueck=None,
        ort="Bremen-Vegesack",
        we_gesamt=30,
        we_miete=24,
        we_gefoerdert_anzahl=10,
        we_gefoerdert=True,
        quote_gefoerdert=0.33,
        ausfuehrungszeitraum="Auslegung bis 11/2026, Baubeginn 2028",
        antragsteller="Bremische Gesellschaft für Stadterneuerung mbH",
        kurzfassung=(
            "Wohnbebauung mit 30 Einheiten (24 Miete, davon 10 gefördert) und 6 "
            "Eigentumswohnungen auf einem ehemaligen Werftgelände."
        ),
        wohnform=Wohnform.GEMISCHT,
        verfahrensstand=Verfahrensstand.AUSLEGUNG,
        konfidenz=0.72,
        evidenz="Geplant sind 24 Mietwohnungen, davon 10 öffentlich gefördert, sowie 6 Eigentumswohnungen.",
        seite=4,
        status="In Prüfung",
    ),
    dict(
        quelle_id="demo_rostock",
        bundesland="MV",
        kommune="Rostock",
        gremium="Ausschuss für Stadtentwicklung",
        sitzungsdatum=date(2026, 9, 9),
        sitzung_url="https://www.rostock.de/ratsinfo/demo/sitzung-2026-09-09",
        vorlagen_nr="2025-IV-112",
        projektbezeichnung="Baugebiet Warnow Quartier",
        strasse="Werftstraße",
        hausnummer="3",
        flurstueck="56/2",
        ort="Rostock",
        we_gesamt=56,
        we_miete=40,
        we_gefoerdert_anzahl=16,
        we_gefoerdert=True,
        quote_gefoerdert=0.29,
        ausfuehrungszeitraum="Aufstellungsbeschluss 09/2026, weiterer Zeitplan offen",
        antragsteller="WIRO Wohnen in Rostock GmbH",
        kurzfassung=(
            "Aufstellungsbeschluss für ein neues Wohnquartier am ehemaligen Werftgelände mit "
            "56 Einheiten, davon 16 gefördert."
        ),
        wohnform=Wohnform.GEMISCHT,
        verfahrensstand=Verfahrensstand.AUFSTELLUNGSBESCHLUSS,
        konfidenz=0.65,
        evidenz="Das Plangebiet soll rund 56 Wohneinheiten aufnehmen, davon ca. 16 im geförderten Segment.",
        seite=1,
        status="Neu",
    ),
    dict(
        quelle_id="demo_berlin_pankow",
        bundesland="BE",
        kommune="Berlin-Pankow",
        gremium="Ausschuss für Stadtentwicklung",
        sitzungsdatum=date(2026, 7, 29),
        sitzung_url="https://www.berlin.de/ba-pankow/ratsinfo/demo/sitzung-2026-07-29",
        vorlagen_nr="1234/XX",
        projektbezeichnung="Wohnbebauung Blankenburger Süden",
        strasse="Blankenburger Chaussee",
        hausnummer="200",
        flurstueck=None,
        ort="Berlin-Pankow",
        we_gesamt=78,
        we_miete=78,
        we_gefoerdert_anzahl=35,
        we_gefoerdert=True,
        quote_gefoerdert=0.45,
        ausfuehrungszeitraum="Auslegung 2026, Satzungsbeschluss frühestens 2028",
        antragsteller="Berlin GmbH / degewo AG",
        kurzfassung=(
            "Teilbereich des Entwicklungsgebiets Blankenburger Süden mit 78 Mietwohnungen, "
            "davon 35 gefördert - Berlin wird laut CLAUDE.md nur selektiv angenommen, Relevanz "
            "bitte mit dem Vertrieb prüfen."
        ),
        wohnform=Wohnform.GEMISCHT,
        verfahrensstand=Verfahrensstand.AUSLEGUNG,
        konfidenz=0.58,
        evidenz="In diesem Baufeld entstehen 78 Wohnungen, davon 35 mit Mietpreis- und Belegungsbindung.",
        seite=7,
        status="Neu",
    ),
    dict(
        quelle_id="norderstedt",
        bundesland="SH",
        kommune="Norderstedt",
        gremium="Ausschuss für Stadtentwicklung und Verkehr",
        sitzungsdatum=date(2026, 9, 15),
        sitzung_url="https://buergerinfo.norderstedt.de/ratsinfo/sessionnet/buergerinfo/si0057.php?__ksinr=8902",
        vorlagen_nr="A 26/0412",
        projektbezeichnung="Studierendenwohnen Harkshörn",
        strasse="Harkshörner Weg",
        hausnummer="9",
        flurstueck=None,
        ort="Norderstedt",
        we_gesamt=60,
        we_miete=60,
        we_gefoerdert_anzahl=None,
        we_gefoerdert=None,
        quote_gefoerdert=None,
        ausfuehrungszeitraum="Baugenehmigung erteilt, Baubeginn Q1 2027",
        antragsteller="Studierendenwerk Hamburg (Standort Norderstedt)",
        kurzfassung=(
            "Neubau eines Studierendenwohnheims mit 60 Mikroapartments; Förderquote aus den "
            "vorliegenden Unterlagen nicht ersichtlich."
        ),
        wohnform=Wohnform.SONDERWOHNFORM,
        verfahrensstand=Verfahrensstand.BAUGENEHMIGUNG,
        konfidenz=0.80,
        evidenz="Das Vorhaben umfasst 60 Mikroapartments für Studierende.",
        seite=2,
        status="Kontaktversuch",
    ),
    dict(
        # Bewusst unklarer Fall: fehlende Förderzahl/Wohnform -> "prüfen" (CLAUDE.md: "unklare
        # Fälle nie stillschweigend verwerfen"), auch ohne Projektbezeichnung (zeigt app.py-
        # Fallback "(ohne Bezeichnung)" in der Vorkommen-Auswahl).
        quelle_id="demo_bad_oldesloe",
        bundesland="SH",
        kommune="Bad Oldesloe",
        gremium="Bauausschuss",
        sitzungsdatum=date(2026, 9, 1),
        sitzung_url="https://www.bad-oldesloe.de/ratsinfo/demo/sitzung-2026-09-01",
        vorlagen_nr="2026/034",
        projektbezeichnung=None,
        strasse="Möllner Straße",
        hausnummer="40",
        flurstueck=None,
        ort="Bad Oldesloe",
        we_gesamt=12,
        we_miete=None,
        we_gefoerdert_anzahl=None,
        we_gefoerdert=None,
        quote_gefoerdert=None,
        ausfuehrungszeitraum=None,
        antragsteller=None,
        kurzfassung=(
            "Kurzer Hinweis auf ein Wohnbauvorhaben in der Beschlussvorlage; genaue Aufteilung "
            "Miete/Eigentum und Förderanteil aus dem Text nicht sicher zu entnehmen - zur "
            "Prüfung markiert."
        ),
        wohnform=Wohnform.UNKLAR,
        verfahrensstand=Verfahrensstand.VORBERATUNG,
        konfidenz=0.35,
        evidenz="Es sollen ca. 12 Wohneinheiten entstehen; weitere Angaben liegen nicht vor.",
        seite=1,
        status="Neu",
    ),
]


def demo_db_path(settings: Settings) -> Path:
    """Liegt in einem `demo/`-Unterordner neben der echten Datenbank (`settings.database.path`),
    NICHT fest gegen `PROJECT_ROOT` verankert: Tests übergeben eine `Settings`-Instanz mit
    `database.path` in `tmp_path` - eine feste `PROJECT_ROOT`-Auflösung hätte diese Tests
    unbemerkt gegen die echte `data/demo/demo.db` im Projektverzeichnis laufen lassen statt gegen
    eine isolierte Kopie (so tatsächlich passiert: `seed()`s abschließendes
    `set_projekt_status()` läuft bei jedem Testlauf erneut und häufte in der echten Demo-Datei
    Dutzende überflüssige `status_log`-Einträge an, sichtbar im "Statusverlauf"-Aufklapp-Bereich)."""
    return settings.resolve_path(settings.database.path).parent / "demo" / "demo.db"


def demo_settings(settings: Settings) -> Settings:
    """Eigenständige `Settings` mit demselben Rest, aber `database.path` auf die Demo-Datei
    umgebogen - so bleiben `radar.database`s Verbindungsaufbau/Migration unverändert wiederverwendbar."""
    return replace(settings, database=DatabaseConfig(path=str(demo_db_path(settings))))


def seed(conn: sqlite3.Connection) -> None:
    """Demo-Quellen/-Projekte in eine bereits verbundene (migrierte) SQLite-Verbindung schreiben.
    Nutzt dieselben `database.upsert_*()`-Funktionen wie ein echter Lauf und ist dadurch genauso
    idempotent - beliebig oft aufrufbar, ohne Duplikate."""
    for quelle_id, name, bundesland, system in _QUELLEN:
        conn.execute(
            "INSERT OR IGNORE INTO quellen (id, name, bundesland, system) VALUES (?, ?, ?, ?)",
            (quelle_id, name, bundesland, system),
        )
    conn.commit()

    for i, eintrag in enumerate(_PROJEKTE, start=1):
        sitzung = Sitzung(
            source_id=eintrag["quelle_id"],
            external_id=f"demo-{i}",
            gremium=eintrag["gremium"],
            datum=eintrag["sitzungsdatum"],
            url=eintrag["sitzung_url"],
        )
        sitzung_id = database.upsert_sitzung(conn, eintrag["quelle_id"], sitzung)
        vorlage_id, _ = database.upsert_vorlage(
            conn,
            eintrag["quelle_id"],
            sitzung_id,
            f"demo-vorlage-{i}",
            eintrag["vorlagen_nr"],
            f"demo-hash-{i}",
        )
        extraktion = ProjektExtraktion(
            projektbezeichnung=eintrag["projektbezeichnung"],
            adresse=Adresse(
                strasse=eintrag["strasse"],
                hausnummer=eintrag["hausnummer"],
                flurstueck=eintrag["flurstueck"],
                ort=eintrag["ort"],
            ),
            we_gesamt=eintrag["we_gesamt"],
            we_miete=eintrag["we_miete"],
            we_gefoerdert_anzahl=eintrag["we_gefoerdert_anzahl"],
            we_gefoerdert=eintrag["we_gefoerdert"],
            quote_gefoerdert=eintrag["quote_gefoerdert"],
            ausfuehrungszeitraum=eintrag["ausfuehrungszeitraum"],
            antragsteller=eintrag["antragsteller"],
            kurzfassung=eintrag["kurzfassung"],
            wohnform=eintrag["wohnform"],
            verfahrensstand=eintrag["verfahrensstand"],
            konfidenz=eintrag["konfidenz"],
            evidenz=eintrag["evidenz"],
            seite=eintrag["seite"],
        )
        projekt_id, _ = database.upsert_projekt(
            conn,
            quelle_id=eintrag["quelle_id"],
            bundesland=eintrag["bundesland"],
            kommune=eintrag["kommune"],
            extraktion=extraktion,
        )
        conn.execute(
            "INSERT OR IGNORE INTO projekt_vorlage (projekt_id, vorlage_id) VALUES (?, ?)",
            (projekt_id, vorlage_id),
        )
        conn.commit()
        # Nur setzen, wenn nötig: set_projekt_status() schreibt bei jedem Aufruf einen neuen
        # status_log-Eintrag, auch wenn sich der Status gar nicht ändert. Ein wiederholter
        # seed()-Aufruf (manueller Reset über scripts/seed_demo_data.py) würde sonst bei jedem
        # Lauf einen weiteren "Neu -> Neu"-Eintrag anhäufen, sichtbar im Statusverlauf.
        aktueller_status = conn.execute(
            "SELECT status FROM projekte WHERE id = ?", (projekt_id,)
        ).fetchone()["status"]
        if aktueller_status != eintrag["status"]:
            database.set_projekt_status(conn, projekt_id, eintrag["status"])


def ensure_demo_seeded(settings: Settings) -> Settings:
    """Demo-`Settings` liefern und die Demo-Datenbank beim allerersten Mal (noch keine Projekte
    darin) befüllen. Spätere Aufrufe lassen bereits vorhandene Daten (inkl. im Dashboard bearbeiteter
    Status/Notizen) unangetastet - ein manueller Reset läuft über `scripts/seed_demo_data.py`."""
    demo = demo_settings(settings)
    conn = database.connect(demo)
    try:
        anzahl = conn.execute("SELECT COUNT(*) FROM projekte").fetchone()[0]
        if anzahl == 0:
            seed(conn)
    finally:
        conn.close()
    return demo


def reset_demo(settings: Settings) -> Settings:
    """Demo-Datenbank unbedingt auf die kanonischen Beispielwerte zurücksetzen (Sidebar-Button
    "Demo-Daten zurücksetzen"). Anders als `ensure_demo_seeded()` (befüllt nur beim allerersten
    Mal) läuft `seed()` hier immer, auch wenn die Demo-DB schon Daten enthält - das ist hier
    gerade der Zweck. `seed()` setzt dabei bereits Status zurück (siehe dort), fasst aber
    `notizen` nicht an; hier zusätzlich, weil ein expliziter Reset genau dafür da ist, im
    Dashboard vorgenommene Demo-Kritzeleien wieder in den Ausgangszustand zu bringen - anders als
    bei einem echten Lauf, wo `upsert_projekt()` manuell gepflegte Felder bewusst nie überschreibt
    (CLAUDE.md)."""
    demo = demo_settings(settings)
    conn = database.connect(demo)
    try:
        seed(conn)
        for projekt in database.list_projekte(conn):
            if projekt["notizen"]:
                database.set_projekt_notizen(conn, projekt["id"], "")
    finally:
        conn.close()
    return demo
