"""Recall gegen Hummels Liste bekannter Norderstedter Projekte (radar/recall.py).

Zwei Teile:
1. Logik-Tests (immer, ohne Netz): wann gilt ein bekanntes Projekt als gefunden?
2. Die eigentliche Messung gegen die echte Datenbank (`data/radar.db`), Marker `recall`. Sie läuft NICHT bei
   einem normalen `pytest` (abhängig vom Stand der echten Daten, siehe pyproject.toml), sondern gezielt:

       pytest -m recall

   Ergebnis "Das Radar findet X von Y bekannten Projekten" steht am Ende der Ausgabe, die fehlenden als
   FAILED. Projekte, die nur über die Presse auffindbar sind, sind xfail, bis es dafür eine Quelle gibt.
"""

import sqlite3
from dataclasses import replace

import pytest

from radar import database, recall
from radar.config import DatabaseConfig, load_settings
from radar.parser import Adresse, ProjektExtraktion, Wohnform

# -- Liste ----------------------------------------------------------------------------------------


def test_liste_enthaelt_hummels_projekte_und_garstedter_tor():
    bekannte = recall.lade_bekannte_projekte()
    assert len(bekannte) == 18
    namen = {p.name for p in bekannte}
    assert "Garstedter Tor (B-Plan 337)" in namen
    assert {p.name for p in bekannte if p.nur_presse} == {"Falkenkamp 4", "Königsberger Straße", "Liesel-Hünichen-Weg"}
    assert all(p.suche for p in bekannte)


# -- Logik ----------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = database.connect(replace(load_settings(), database=DatabaseConfig(path=str(tmp_path / "radar.db"))))
    for quelle_id in ("norderstedt", "echte_projekte", "kiel"):
        connection.execute(
            "INSERT INTO quellen (id, name, bundesland, system) VALUES (?, 'x', 'SH', 'sessionnet')", (quelle_id,)
        )
    connection.commit()
    yield connection
    connection.close()


def _projekt(conn, *, bezeichnung=None, strasse=None, hausnummer=None, kurzfassung=None, quelle_id="norderstedt",
             kommune="Stadt Norderstedt"):
    database.upsert_projekt(
        conn,
        quelle_id=quelle_id,
        bundesland="SH",
        kommune=kommune,
        extraktion=ProjektExtraktion(
            projektbezeichnung=bezeichnung,
            adresse=Adresse(
                strasse=strasse, hausnummer=hausnummer, ort="Norderstedt" if kommune.endswith("Norderstedt") else None
            ),
            kurzfassung=kurzfassung,
            wohnform=Wohnform.UNKLAR,
            konfidenz=0.8,
        ),
    )


def _bekannt(*suche, nur_presse=False):
    return recall.BekanntesProjekt(name="X", suche=suche, nur_presse=nur_presse)


def test_strassenname_trotz_schreibweise_str_strasse(conn):
    _projekt(conn, bezeichnung="Wohnen am Bahnhof", strasse="Schweriner Straße", hausnummer="5")
    _projekt(conn, bezeichnung="Postgelände", strasse="Falkenbergstr.", hausnummer="17-23")
    ergebnis = recall.messe_recall(conn, [_bekannt("Schweriner Str"), _bekannt("Falkenbergstr")])
    assert len(ergebnis.gefunden) == 2


def test_adresse_mit_hausnummer(conn):
    _projekt(conn, strasse="Ulzburger Str.", hausnummer="457–463")
    ergebnis = recall.messe_recall(conn, [_bekannt("Ulzburger Straße 457")])
    assert len(ergebnis.gefunden) == 1


def test_hausnummer_trifft_keine_laengere_zahl(conn):
    _projekt(conn, strasse="Berliner Allee", hausnummer="45")
    assert recall.messe_recall(conn, [_bekannt("Berliner Allee 4")]).fehlend


def test_zahl_ist_bplan_nummer_nicht_we_zahl(conn):
    _projekt(conn, bezeichnung="Wohnquartier Ohechaussee", kurzfassung="Es entstehen 322 Wohneinheiten.")
    assert recall.messe_recall(conn, [_bekannt("322")]).fehlend
    _projekt(conn, bezeichnung="Bebauungsplan Nr. 322 Norderstedt „An der Ohechaussee“")
    assert recall.messe_recall(conn, [_bekannt("322")]).gefunden


def test_bplan_schreibweisen(conn):
    _projekt(conn, bezeichnung="B-Plan 339 zwischen Berliner Allee und Schumanstraße", strasse="Berliner Allee")
    _projekt(
        conn,
        bezeichnung="Quartier West",
        strasse="Kohfurth",
        kurzfassung="Aufstellung des Bebauungsplanes Nr.337 beschlossen",
    )
    ergebnis = recall.messe_recall(conn, [_bekannt("339"), _bekannt("337")])
    assert len(ergebnis.gefunden) == 2


def test_manuell_nachgetragene_projekte_zaehlen_nicht(conn):
    _projekt(conn, bezeichnung="Richtweg / Garstedter Dreieck", strasse="Richtweg", quelle_id="echte_projekte")
    assert recall.messe_recall(conn, [_bekannt("Richtweg")]).fehlend


def test_herkunft_spalte_wird_beachtet_wenn_vorhanden(conn):
    _projekt(conn, bezeichnung="Richtweg 28", strasse="Richtweg")
    assert recall.messe_recall(conn, [_bekannt("Richtweg")]).gefunden  # Standard: 'radar'
    conn.execute("UPDATE projekte SET herkunft = 'manuell_nachgetragen'")
    assert recall.messe_recall(conn, [_bekannt("Richtweg")]).fehlend


def test_andere_kommune_zaehlt_nicht(conn):
    _projekt(conn, bezeichnung="Richtweg Kiel", strasse="Richtweg", quelle_id="kiel", kommune="Landeshauptstadt Kiel")
    assert recall.messe_recall(conn, [_bekannt("Richtweg")]).fehlend


def test_web_fund_zaehlt(conn):
    conn.execute(
        """INSERT INTO web_funde (url, titel, auszug, domain, ort, bundesland, entfernung_km, phase, suchanfrage,
                                  erstellt_am, aktualisiert_am)
           VALUES ('https://example.org/a', 'Spatenstich für 69 geförderte Wohnungen', 'am Liesel-Hünichen-Weg',
                   'example.org', 'Norderstedt', 'SH', 0, 'BAUBEGINN', 'q', '2026-11-13', '2026-11-13')"""
    )
    ergebnis = recall.messe_recall(conn, [_bekannt("Hünichen", nur_presse=True)])
    assert ergebnis.gefunden and "Web-Fund" in ergebnis.gefunden[0][1]


def test_bericht_nennt_zahl_und_fehlende(conn):
    _projekt(conn, strasse="Harkshörner Weg")
    ergebnis = recall.messe_recall(conn, [_bekannt("Harkshörner Weg"), _bekannt("Falkenkamp", nur_presse=True)])
    text = recall.bericht(ergebnis)
    assert "findet 1 von 2" in text
    assert "nur über Presse" in text
    text.encode("cp1252")  # Windows-Konsole muss den Bericht ausgeben können


def test_cli_recall_liest_nur(tmp_path, capsys, conn):
    from radar.cli import main

    pfad = tmp_path / "radar.db"
    assert main(["recall", "--db", str(pfad)]) == 0
    assert "von 18 bekannten Projekten" in capsys.readouterr().out
    assert main(["recall", "--db", str(tmp_path / "fehlt.db")]) == 2


# -- Messung gegen die echte Datenbank (nur mit `pytest -m recall`) ----------------------------------

_BEKANNTE = recall.lade_bekannte_projekte()
_ERGEBNIS: dict[str, recall.RecallErgebnis] = {}


def _echtes_ergebnis() -> recall.RecallErgebnis:
    if "e" not in _ERGEBNIS:
        settings = load_settings()
        pfad = settings.resolve_path(settings.database.path)
        if not pfad.exists():
            pytest.skip(f"keine Datenbank unter {pfad} - erst einen Lauf starten")
        verbindung = sqlite3.connect(f"file:{pfad.as_posix()}?mode=ro", uri=True)
        try:
            _ERGEBNIS["e"] = recall.messe_recall(verbindung, _BEKANNTE)
        finally:
            verbindung.close()
        recall.LETZTER_BERICHT = recall.bericht(_ERGEBNIS["e"])  # für die Zusammenfassung (conftest.py)
    return _ERGEBNIS["e"]


@pytest.mark.recall
@pytest.mark.parametrize(
    "bekannt",
    [
        pytest.param(
            p,
            id=p.name,
            marks=pytest.mark.xfail(reason="nur über Presse auffindbar - Quelle fehlt noch (Phase 6)", strict=False)
            if p.nur_presse
            else (),
        )
        for p in _BEKANNTE
    ],
)
def test_radar_findet_bekanntes_projekt(bekannt):
    ergebnis = _echtes_ergebnis()
    assert bekannt not in ergebnis.fehlend, f"{ergebnis.zusammenfassung()} - fehlt: {bekannt.name}"


# -- zweite Zahl: geprüfte Ratsvorlage bereits geladen (19.11.2026) ---------------------------------------


def test_liste_hat_neun_gepruefte_ratsvorlagen():
    mit = {p.name: p.ratsvorlage for p in recall.lade_bekannte_projekte() if p.ratsvorlage}
    assert len(mit) == 12
    assert mit["Garstedter Tor (B-Plan 337)"] == "B 24/0295"
    assert not any(p.ratsvorlage for p in recall.lade_bekannte_projekte() if p.nur_presse)


def test_unterlagen_zaehlen_nur_geladene_gepruefte_vorlagen(conn, tmp_path):
    from datetime import date

    from radar.sources.base import Dokument, Sitzung

    sitzung_id = database.upsert_sitzung(
        conn, "norderstedt",
        Sitzung(source_id="norderstedt", external_id="1", gremium="ASV", datum=date(2024, 9, 5), url="si.php"),
    )
    for nr, ext, geladen in [("B 24/0295", "v1", True), ("B 25/0499", "v2", False)]:
        vorlage_id, _ = database.upsert_vorlage(conn, "norderstedt", sitzung_id, ext, nr, f"hash-{ext}")
        dok_id = database.upsert_dokument(
            conn, "norderstedt", sitzung_id, vorlage_id,
            Dokument(source_id="norderstedt", external_id=f"d{ext}", titel="Vorlage", url="getfile.php",
                     sitzung_external_id="1", vorlage_external_id=ext),
        )
        if geladen:
            database.mark_dokument_heruntergeladen(conn, dok_id, "abc", str(tmp_path / "x.pdf"))

    bekannte = [
        recall.BekanntesProjekt(name="Garstedter Tor", suche=("GARTO",), ratsvorlage="B 24/0295"),
        recall.BekanntesProjekt(name="Segeberger Ch.", suche=("REWE",), ratsvorlage="B 25/0499"),  # PDF fehlt
        recall.BekanntesProjekt(name="Falkenkamp", suche=("Falkenkamp",), nur_presse=True),
    ]
    funde = recall.messe_unterlagen(conn, bekannte)
    assert list(funde) == ["Garstedter Tor"]
    assert "B 24/0295 (2024-09-05, ASV)" in funde["Garstedter Tor"]

    text = recall.bericht(recall.messe_recall(conn, bekannte), funde)
    assert "findet 0 von 3" in text and "Ratsunterlagen bereits geladen: 1 von 3" in text
    text.encode("cp1252")
