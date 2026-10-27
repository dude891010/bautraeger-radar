"""Tests für radar/web_recherche.py: Konfiguration, Umkreis, Bewertung von Treffern (ohne LLM),
Tavily-Client (per requests-mock, kein Netz), Lauf mit Kostenbremse und Speicherung in `web_funde`.

Die Tavily-Antworten unten sind synthetisch, im dokumentierten Format der Search-API (`results[]`
mit `title`/`url`/`content`/`score`) - ein echter Abruf war mangels API-Key noch nicht möglich."""

from contextlib import closing
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest
import requests

from app import WEB_PHASE_BADGE, apply_web_filter, build_web_funde_df, web_export_view
from radar import database, demo_data
from radar.cli import main
from radar.config import (
    DashboardConfig,
    DatabaseConfig,
    FiltersConfig,
    LLMConfig,
    ScanConfig,
    ScraperConfig,
    Settings,
)
from radar.web_recherche import (
    MAX_ANFRAGEN_HARD_LIMIT,
    Ort,
    Phase,
    Suchauftrag,
    SucheGesperrt,
    Suchergebnis,
    TavilyClient,
    WebFund,
    WebRechercheConfig,
    baue_suchauftraege,
    bewerte_ergebnis,
    entfernung_km,
    extrahiere_we,
    fuehre_recherche_aus,
    klassifiziere_phase,
    load_web_config,
    normalisiere_url,
    orte_im_radius,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(),
        filters=FiltersConfig(),
        llm=LLMConfig(),
        database=DatabaseConfig(path=str(tmp_path / "radar.db")),
        dashboard=DashboardConfig(),
    )


@pytest.fixture
def conn(settings):
    with closing(database.connect(settings)) as c:
        yield c


@pytest.fixture
def config(settings):
    """Die echte config/web_recherche.yaml, aber ohne Wartezeit zwischen Anfragen."""
    return replace(load_web_config(settings), request_delay_seconds=0)


QUICKBORN = Ort(name="Quickborn", bundesland="SH", lat=53.7295, lon=9.9046)


def _auftrag(ort=QUICKBORN, phase=Phase.BAUGENEHMIGUNG):
    return Suchauftrag(ort=ort, entfernung_km=6.3, phase=phase, anfrage=f"{ort.name} Baugenehmigung")


def _ergebnis(titel="", inhalt="", url="https://www.abendblatt.de/region/quickborn-neubau.html"):
    return Suchergebnis(titel=titel, url=url, inhalt=inhalt)


class FakeAnbieter:
    def __init__(self, antworten=None, fehler=None):
        self.antworten = antworten or {}
        self.fehler = fehler
        self.anfragen: list[str] = []

    def suche(self, anfrage):
        self.anfragen.append(anfrage)
        if self.fehler is not None:
            raise self.fehler
        return self.antworten.get(anfrage, [])


# -- Konfiguration / Umkreis -----------------------------------------------------------------------


def test_echte_konfiguration_laedt_und_hat_beide_phasen(config):
    assert config.radius_km == 50
    assert set(config.suchanfragen) == {"BAUGENEHMIGUNG", "BAUBEGINN"}
    assert config.max_anfragen_pro_lauf <= MAX_ANFRAGEN_HARD_LIMIT


def test_entfernung_norderstedt_hamburg_rathaus_plausibel():
    # Norderstedt-Mitte -> Hamburger Rathaus: rund 17-18 km Luftlinie
    assert 16 < entfernung_km(53.7064, 9.9922, 53.5503, 9.9930) < 19


def test_orte_ausserhalb_des_radius_werden_ignoriert(config):
    namen = [ort.name for ort, _ in orte_im_radius(config)]
    assert "Norderstedt" in namen and "Hamburg-Eimsbüttel" in namen
    assert "Tostedt" not in namen  # knapp über 50 km
    entfernungen = [e for _, e in orte_im_radius(config)]
    assert entfernungen == sorted(entfernungen)  # nächster Ort zuerst
    assert max(entfernungen) <= 50


def test_suchauftraege_je_ort_und_phase(config):
    auftraege = baue_suchauftraege(config)
    assert len(auftraege) == 2 * len(orte_im_radius(config))
    buchholz = [a for a in auftraege if a.ort.name == "Buchholz"]
    assert all("Buchholz in der Nordheide" in a.anfrage for a in buchholz)  # suchname greift


def test_konfiguration_lehnt_zu_hohes_budget_ab():
    with pytest.raises(ValueError, match="max_anfragen_pro_lauf"):
        WebRechercheConfig(max_anfragen_pro_lauf=MAX_ANFRAGEN_HARD_LIMIT + 1)


def test_konfiguration_lehnt_unbekannte_phase_ab():
    with pytest.raises(ValueError, match="unbekannte Phase"):
        WebRechercheConfig(suchanfragen={"AUFSTELLUNGSBESCHLUSS": ["{ort} B-Plan"]})


def test_konfiguration_lehnt_ort_ohne_platzhalter_ab():
    with pytest.raises(ValueError, match="ort"):
        WebRechercheConfig(suchanfragen={"BAUBEGINN": ["Baubeginn Wohnungen"]})


def test_unbekannter_schluessel_in_yaml_ist_fehler(settings, tmp_path):
    pfad = tmp_path / "web.yaml"
    pfad.write_text("radius_kmm: 30\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unbekannte"):
        load_web_config(settings, pfad)


# -- Bewertung ohne LLM ----------------------------------------------------------------------------


def test_baubeginn_schlaegt_baugenehmigung(config):
    text = "Nach Erteilung der Baugenehmigung war jetzt Spatenstich für 40 Wohnungen."
    assert klassifiziere_phase(text, config) is Phase.BAUBEGINN
    assert klassifiziere_phase("Die Baugenehmigung liegt vor.", config) is Phase.BAUGENEHMIGUNG
    assert klassifiziere_phase("Der Bebauungsplan wurde beschlossen.", config) is None


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Es entstehen 48 neue Mietwohnungen am Bahnhof.", 48),
        ("Insgesamt 36 öffentlich geförderte Wohneinheiten", 36),
        ("Das Quartier umfasst 1.200 Wohnungen.", 1200),
        ("Baubeginn 2026, Fertigstellung 2028.", None),
        ("Eine Wohnung im Erdgeschoss", None),
    ],
)
def test_extrahiere_we(text, erwartet):
    zahl, zitat = extrahiere_we(text)
    assert zahl == erwartet
    if erwartet is not None:
        assert str(erwartet) in zitat.replace(".", "")
        assert len(zitat) <= 200


def test_relevanter_treffer_wird_fund(config):
    ergebnis = _ergebnis(
        titel="Quickborn: Baugenehmigung für Wohnquartier erteilt",
        inhalt="Geplant sind 38 Wohnungen in drei Mehrfamilienhäusern.",
        url="https://www.abendblatt.de/region/quickborn-neubau.html?utm_source=x#kommentare",
    )
    fund = bewerte_ergebnis(ergebnis, _auftrag(), config)
    assert fund is not None
    assert fund.phase is Phase.BAUGENEHMIGUNG
    assert fund.we_anzahl == 38
    assert fund.domain == "abendblatt.de"
    assert fund.url == "https://www.abendblatt.de/region/quickborn-neubau.html"


def test_treffer_ohne_ort_wird_verworfen(config):
    ergebnis = _ergebnis(titel="Baugenehmigung für 38 Wohnungen in Kiel")
    assert bewerte_ergebnis(ergebnis, _auftrag(), config) is None


def test_ort_nur_mit_wortgrenze():
    stade = Ort(name="Stade", bundesland="NI", lat=53.6, lon=9.48)
    cfg = WebRechercheConfig(
        phasen_stichworte={"BAUBEGINN": ["Baubeginn"]}, wohn_stichworte=["Wohnungen"]
    )
    fremd = _ergebnis(titel="Baubeginn für 20 Wohnungen an der Stader Straße in Buxtehude")
    passend = _ergebnis(titel="Stade: Baubeginn für 20 Wohnungen")
    assert bewerte_ergebnis(fremd, _auftrag(stade), cfg) is None
    assert bewerte_ergebnis(passend, _auftrag(stade), cfg) is not None


def test_treffer_ohne_wohnbau_wird_verworfen(config):
    ergebnis = _ergebnis(titel="Quickborn: Baugenehmigung für neue Logistikhalle erteilt")
    assert bewerte_ergebnis(ergebnis, _auftrag(), config) is None


def test_treffer_ohne_phase_wird_verworfen(config):
    ergebnis = _ergebnis(titel="Quickborn plant 200 Wohnungen - Bebauungsplan in Vorbereitung")
    assert bewerte_ergebnis(ergebnis, _auftrag(), config) is None


def test_immobilienportal_wird_verworfen(config):
    ergebnis = _ergebnis(
        titel="Quickborn: Neubau, Baubeginn erfolgt - 3-Zimmer-Wohnung kaufen",
        url="https://www.immobilienscout24.de/expose/123",
    )
    assert bewerte_ergebnis(ergebnis, _auftrag(), config) is None


def test_normalisiere_url():
    assert normalisiere_url("HTTPS://Example.org/a/?utm_medium=x&id=5#top") == "https://example.org/a?id=5"


# -- Tavily-Client (requests-mock, kein Netz) -------------------------------------------------------


def test_tavily_client_sendet_erwartete_anfrage(config, requests_mock):
    requests_mock.post(
        TavilyClient.URL,
        json={
            "query": "Quickborn Baugenehmigung",
            "results": [
                {"title": "Titel", "url": "https://example.org/a", "content": "Inhalt", "score": 0.8},
                {"title": "ohne URL", "url": "javascript:alert(1)", "content": ""},
            ],
        },
    )
    ergebnisse = TavilyClient("tvly-test", config).suche("Quickborn Baugenehmigung")
    assert [e.url for e in ergebnisse] == ["https://example.org/a"]
    anfrage = requests_mock.last_request
    assert anfrage.headers["Authorization"] == "Bearer tvly-test"
    body = anfrage.json()
    assert body["query"] == "Quickborn Baugenehmigung"
    assert body["country"] == "germany"
    assert body["time_range"] == config.zeitraum
    assert "immobilienscout24.de" in body["exclude_domains"]


@pytest.mark.parametrize("status", [401, 432])
def test_tavily_konto_fehler_sperrt(config, requests_mock, status):
    requests_mock.post(TavilyClient.URL, status_code=status, text="limit")
    with pytest.raises(SucheGesperrt):
        TavilyClient("tvly-test", config).suche("x")


def test_tavily_serverfehler_wird_weitergereicht(config, requests_mock):
    requests_mock.post(TavilyClient.URL, status_code=503)
    with pytest.raises(requests.HTTPError):
        TavilyClient("tvly-test", config).suche("x")


# -- Lauf -------------------------------------------------------------------------------------------


def _nur_quickborn(config, **kwargs):
    return replace(config, orte=[QUICKBORN], **kwargs)


def test_lauf_speichert_funde_und_ist_idempotent(conn, config):
    cfg = _nur_quickborn(config)
    anfrage = baue_suchauftraege(cfg)[0].anfrage
    anbieter = FakeAnbieter(
        {
            anfrage: [
                _ergebnis(titel="Quickborn: Baugenehmigung für 38 Wohnungen erteilt"),
                _ergebnis(titel="Wetter in Quickborn", url="https://example.org/wetter"),
            ]
        }
    )
    erstes = fuehre_recherche_aus(conn, cfg, anbieter, heute=date(2026, 10, 27))
    assert erstes.anfragen == 2  # je Phase eine Anfrage
    assert (erstes.funde, erstes.neue_funde, erstes.verworfen) == (1, 1, 1)

    zweites = fuehre_recherche_aus(conn, cfg, anbieter, heute=date(2026, 10, 27))
    assert (zweites.funde, zweites.neue_funde) == (1, 0)
    assert len(database.list_web_funde(conn)) == 1
    assert database.such_anfragen_am(conn, "2026-10-27") == 4


def test_lauf_stoppt_bei_budget_pro_lauf(conn, config):
    anbieter = FakeAnbieter()
    ergebnis = fuehre_recherche_aus(conn, replace(config, max_anfragen_pro_lauf=3), anbieter)
    assert len(anbieter.anfragen) == 3
    assert "Budget" in ergebnis.abbruch_grund


def test_lauf_stoppt_bei_tageslimit_ueber_laeufe(conn, config):
    cfg = replace(config, max_anfragen_pro_tag=5)
    heute = date(2026, 10, 27)
    fuehre_recherche_aus(conn, replace(cfg, max_anfragen_pro_lauf=4), FakeAnbieter(), heute=heute)
    anbieter = FakeAnbieter()
    ergebnis = fuehre_recherche_aus(conn, cfg, anbieter, heute=heute)
    assert len(anbieter.anfragen) == 1
    assert "Tageslimit" in ergebnis.abbruch_grund


def test_konto_fehler_stoppt_sofort(conn, config):
    anbieter = FakeAnbieter(fehler=SucheGesperrt("Tavily HTTP 432: Plan-Limit"))
    ergebnis = fuehre_recherche_aus(conn, config, anbieter)
    assert len(anbieter.anfragen) == 1
    assert not ergebnis.ok


def test_notbremse_nach_fehlern_in_folge(conn, config):
    anbieter = FakeAnbieter(fehler=requests.Timeout("zu langsam"))
    ergebnis = fuehre_recherche_aus(conn, config, anbieter)
    assert len(anbieter.anfragen) == config.max_fehler_in_folge
    assert "Fehler in Folge" in ergebnis.abbruch_grund


# -- Datenbank: manuelle Felder bleiben, Phase nur hochstufen ---------------------------------------


def _fund(**kwargs):
    werte = dict(
        url="https://example.org/a",
        titel="T",
        auszug="A",
        domain="example.org",
        ort="Quickborn",
        bundesland="SH",
        entfernung_km=6.3,
        phase=Phase.BAUBEGINN,
        we_anzahl=38,
        evidenz="38 Wohnungen",
        suchanfrage="q",
    )
    werte.update(kwargs)
    return WebFund(**werte)


def test_upsert_web_fund_ueberschreibt_status_und_notizen_nie(conn):
    fund_id, ist_neu = database.upsert_web_fund(conn, _fund())
    assert ist_neu
    database.set_web_fund_status(conn, fund_id, "Kontaktversuch")
    database.set_web_fund_notizen(conn, fund_id, "Bauträger angerufen")

    _, ist_neu = database.upsert_web_fund(conn, _fund(phase=Phase.BAUGENEHMIGUNG, we_anzahl=None, titel="neu"))
    assert not ist_neu
    row = database.list_web_funde(conn)[0]
    assert row["status"] == "Kontaktversuch"
    assert row["notizen"] == "Bauträger angerufen"
    assert row["phase"] == "BAUBEGINN"  # nicht zurückgestuft
    assert row["we_anzahl"] == 38  # nicht durch "nicht gefunden" gelöscht
    assert row["titel"] == "neu"


def test_upsert_web_fund_stuft_phase_hoch(conn):
    database.upsert_web_fund(conn, _fund(phase=Phase.BAUGENEHMIGUNG))
    database.upsert_web_fund(conn, _fund(phase=Phase.BAUBEGINN))
    assert database.list_web_funde(conn)[0]["phase"] == "BAUBEGINN"


# -- Dashboard-Logik (app.py) ------------------------------------------------------------------------


def _web_df(conn):
    database.upsert_web_fund(conn, _fund(url="https://example.org/1", entfernung_km=6.3))
    database.upsert_web_fund(
        conn, _fund(url="https://example.org/2", ort="Stade", entfernung_km=36.0, phase=Phase.BAUGENEHMIGUNG,
                    we_anzahl=None)
    )
    return build_web_funde_df(database.list_web_funde(conn))


def test_build_web_funde_df_beschriftet_phase(conn):
    df = _web_df(conn)
    assert set(df["phase_label"]) == set(WEB_PHASE_BADGE.values())
    assert str(df["we_anzahl"].dtype) == "Int64"


def test_apply_web_filter(conn):
    df = _web_df(conn)
    assert len(apply_web_filter(df)) == 2
    assert list(apply_web_filter(df, phasen=["BAUBEGINN"])["ort"]) == ["Quickborn"]
    assert list(apply_web_filter(df, max_km=20)["ort"]) == ["Quickborn"]
    assert list(apply_web_filter(df, orte=["Stade"])["ort"]) == ["Stade"]
    assert list(apply_web_filter(df, nur_mit_we=True)["ort"]) == ["Quickborn"]
    assert apply_web_filter(pd.DataFrame(), phasen=["BAUBEGINN"]).empty


def test_web_export_view_spalten(conn):
    export = web_export_view(_web_df(conn))
    assert list(export.columns)[:2] == ["phase_label", "ort"]
    assert "url" in export.columns and "id" not in export.columns


# -- Demo / CLI ------------------------------------------------------------------------------------------


def test_demo_enthaelt_web_funde_und_reset_setzt_zurueck(settings):
    demo = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo)) as c:
        funde = database.list_web_funde(c)
        assert len(funde) >= 3
        assert all(f["url"].startswith("https://example.org/demo/") for f in funde)
        database.set_web_fund_notizen(c, funde[0]["id"], "Notiz")
    demo_data.reset_demo(settings)
    with closing(database.connect(demo)) as c:
        assert all(f["notizen"] == "" for f in database.list_web_funde(c))


def test_cli_ohne_api_key_meldet_fehler_ohne_anfrage(settings, monkeypatch, requests_mock):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    assert main(["web-recherche"]) == 2
    assert not requests_mock.called
