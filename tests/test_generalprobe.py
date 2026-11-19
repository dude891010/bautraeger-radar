"""Korrekturen aus der Demo-Generalprobe (20.11.2026, Dashboard im Browser durchgeklickt). Nur pure Funktionen
aus app.py, ohne laufende Streamlit-Session."""

from datetime import UTC, datetime

import pandas as pd

import app
from radar import demo_data


def test_lauf_hinweis_heute_und_gestern():
    jetzt = datetime(2026, 11, 20, 12, tzinfo=UTC)
    assert app.lauf_hinweis(datetime(2026, 11, 20, 9, tzinfo=UTC), jetzt)[1].endswith("(heute)")
    assert app.lauf_hinweis(datetime(2026, 11, 19, 9, tzinfo=UTC), jetzt)[1].endswith("(gestern)")
    assert "vor 0 Tagen" not in app.lauf_hinweis(datetime(2026, 11, 20, 11, tzinfo=UTC), jetzt)[1]


def test_demo_hinweis_zaehlt_echte_und_erfundene():
    df = pd.DataFrame({"quelle_id": [demo_data.ECHTE_QUELLE_ID] * 15 + ["demo_rostock"] * 9})
    text = app.demo_hinweis(df)
    assert "15 echte" in text and "9 erfundene" in text
    assert "synthetische Beispielprojekte angezeigt, keine echten Daten" not in text


def test_lead_view_zeigt_strich_statt_none_und_laesst_zahlen():
    df = pd.DataFrame({
        "lead": ["🟢"], "herkunft": [""], "projektbezeichnung": [None], "hausnummer": [None],
        "we_gesamt": pd.array([69], dtype="Int64"),
    })
    view = app.lead_view(df)
    assert view.loc[0, "projektbezeichnung"] == "–" and view.loc[0, "hausnummer"] == "–"
    assert view.loc[0, "we_gesamt"] == 69
    assert "herkunft" not in view.columns  # echter Betrieb: leere Herkunft-Spalte entfällt


def test_web_leads_nur_baubeginn_und_genehmigung_ohne_archivierte():
    web = pd.DataFrame({
        "phase": ["BAUGENEHMIGUNG", "BAUBEGINN", "BAUBEGINN", "SONSTIGES"],
        "status": ["Neu", "Neu", "Archiviert", "Neu"],
        "we_anzahl": pd.array([100, 69, 300, 5], dtype="Int64"),
        "titel": ["Genehmigung", "Spatenstich", "alt", "anders"],
    })
    leads = app.web_leads(web)
    assert list(leads["titel"]) == ["Spatenstich", "Genehmigung"]  # Baubeginn zuerst, Archivierte fehlen
    assert app.web_leads(pd.DataFrame()).empty


def test_quellen_view_lesbar_mit_status_und_ortszeit():
    zeilen = [
        {"id": "norderstedt", "name": "Stadt Norderstedt", "system": "sessionnet",
         "letzter_lauf": "2026-11-19T09:59:51+00:00", "letzter_erfolg": "2026-11-19T09:59:51+00:00",
         "letzter_fehler": "2026-11-18T21:33:24+00:00", "erfasst_ab": "2024-01-01",
         "anzahl_sitzungen": 54, "anzahl_vorlagen": 46, "anzahl_dokumente": 259},
        {"id": "kaputt", "name": "Kaputt", "system": "rss", "letzter_lauf": "2026-11-19T10:00:00+00:00",
         "letzter_erfolg": None, "letzter_fehler": "2026-11-19T10:00:00+00:00", "erfasst_ab": None},
        {"id": "neu", "name": "Neu", "system": "oparl", "letzter_lauf": None},
    ]
    view = app.quellen_view(zeilen).set_index("Quelle")
    assert view.loc["Stadt Norderstedt", "Status"] == "✅ OK"
    assert view.loc["Stadt Norderstedt", "Letzter Lauf"] == "19.11.2026 10:59"  # deutsche Zeit (UTC+1)
    assert view.loc["Stadt Norderstedt", "Erfasst ab"] == "01.01.2024"
    assert view.loc["Stadt Norderstedt", "Art"] == "Ratsinformationssystem"
    assert view.loc["Kaputt", "Status"].startswith("⚠")
    assert view.loc["Neu", "Status"].startswith("–")
    assert app.quellen_view([]).empty


def test_multiselect_platzhalter_deutsch():
    gesehen = {}

    class _St:
        def multiselect(self, label, options, **kwargs):
            gesehen.update(kwargs, label=label)
            return []

    app._multiselect(_St(), "Ort", ["Norderstedt"], key="x")
    assert gesehen["placeholder"] == "Alle" and gesehen["key"] == "x"
