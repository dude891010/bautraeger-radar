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
    BetriebConfig,
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
    NORDLICHT_URL,
    VERIFIZIERTE_DEMO_URLS,
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
def live_settings(settings):
    """Live-Betrieb eingeschaltet - nur für Tests, die den Tavily-Weg (per requests-mock) prüfen."""
    return replace(settings, betrieb=BetriebConfig(live_api_aufrufe=True))


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

    assert [f.ort for f in erstes.neu_fuer_mail] == ["Quickborn"]
    assert erstes.neu_fuer_mail[0].phase == "Phase 3 – Baugenehmigung"

    zweites = fuehre_recherche_aus(conn, cfg, anbieter, heute=date(2026, 10, 27))
    assert (zweites.funde, zweites.neue_funde) == (1, 0)
    assert zweites.neu_fuer_mail == []  # bekannte Funde werden nicht erneut gemeldet
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
    assert list(export.columns)[:4] == ["hinweis", "phase_label", "wohnform_label", "ort"]
    assert "url" in export.columns and "id" not in export.columns


# -- Demo / CLI ------------------------------------------------------------------------------------------


def test_demo_enthaelt_web_funde_und_reset_setzt_zurueck(settings):
    demo = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo)) as c:
        funde = database.list_web_funde(c)
        assert len(funde) >= 3
        assert all(f["url"].startswith("https://example.org/demo/") or f["url"] in VERIFIZIERTE_DEMO_URLS
                   for f in funde)
        database.set_web_fund_notizen(c, funde[0]["id"], "Notiz")
    demo_data.reset_demo(settings)
    with closing(database.connect(demo)) as c:
        assert all(f["notizen"] == "" for f in database.list_web_funde(c))


def test_cli_ohne_api_key_meldet_fehler_ohne_anfrage(live_settings, monkeypatch, requests_mock):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: live_settings)
    assert main(["web-recherche"]) == 2
    assert not requests_mock.called



def test_cli_notify_meldet_neue_web_funde(monkeypatch):
    from radar.notifier import NeuerWebFund
    from radar.web_recherche import WebRechercheErgebnis

    fund = NeuerWebFund(ort="Quickborn", phase="Phase 3 – Baugenehmigung", titel="T", we_anzahl=1,
                        entfernung_km=6.3, url="https://example.org/a")
    monkeypatch.setattr("radar.cli.run_web_recherche", lambda **kw: WebRechercheErgebnis(neu_fuer_mail=[fund]))
    aufrufe = []
    monkeypatch.setattr("radar.cli.live_aufrufe_erlaubt", lambda: True)
    monkeypatch.setattr("radar.cli.send_notification", lambda t, **kw: aufrufe.append((t, kw)))
    assert main(["web-recherche"]) == 0
    assert aufrufe == []  # ohne --notify keine Mail
    assert main(["web-recherche", "--notify"]) == 0
    assert aufrufe == [([], {"web_funde": [fund]})]



def test_cli_testsuche_mit_eigener_db(settings, live_settings, monkeypatch, requests_mock, tmp_path, capsys):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: live_settings)
    monkeypatch.setattr("radar.web_recherche._sleep", lambda s: None)
    requests_mock.post(
        TavilyClient.URL,
        json={"results": [{"title": "Quickborn: Baugenehmigung für 38 Wohnungen erteilt",
                           "url": "https://example.org/q", "content": ""}]},
    )
    test_db = tmp_path / "test_radar.db"

    assert main(["web-recherche", "--test", "--db", str(test_db)]) == 0

    assert requests_mock.call_count == 6  # 3 nächste Orte x 2 Phasen
    with closing(database.connect(replace(settings, database=DatabaseConfig(path=str(test_db))))) as c:
        assert [f["ort"] for f in database.list_web_funde(c)] == ["Quickborn"]
    with closing(database.connect(settings)) as c:
        assert database.list_web_funde(c) == []  # Standard-DB unberührt
    assert "Quickborn" in capsys.readouterr().out


# -- Demo-Modus (ohne Key, ohne Credits) --------------------------------------------------------------


def test_demo_funde_durchlaufen_die_echte_bewertung(config):
    from radar.web_recherche import demo_funde

    funde = {f.url: f for f in demo_funde(config)}
    erfundene = [f for url, f in funde.items() if url not in VERIFIZIERTE_DEMO_URLS]
    assert all(f.url.startswith("https://example.org/demo/") and "(Demo)" in f.titel for f in erfundene)
    beispielweg = funde["https://example.org/demo/norderstedt-beispielweg"]
    assert (beispielweg.phase, beispielweg.we_anzahl) == (Phase.BAUGENEHMIGUNG, 96)
    assert funde["https://example.org/demo/norderstedt-musterpark"].phase is Phase.BAUBEGINN
    # bewusst verworfene Beispiele: Gewerbe, Immobilienportal, B-Plan ohne Phase 3/4
    for verworfen in ("norderstedt-logistik", "ellerau-bplan", "quickborn-etw-klein"):
        assert "https://example.org/demo/" + verworfen not in funde
    assert not any("immobilienscout24" in url for url in funde)


def test_cli_test_ohne_key_weicht_auf_demo_aus(settings, monkeypatch, requests_mock, tmp_path, capsys):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    test_db = tmp_path / "test_radar.db"

    assert main(["web-recherche", "--test", "--db", str(test_db)]) == 0

    assert not requests_mock.called  # keine echte Anfrage, kein Credit
    ausgabe = capsys.readouterr().out
    assert "DEMO-MODUS" in ausgabe and "Beispielweg" in ausgabe
    with closing(database.connect(replace(settings, database=DatabaseConfig(path=str(test_db))))) as c:
        orte = {f["ort"] for f in database.list_web_funde(c)}
        assert orte == {"Norderstedt", "Quickborn", "Ellerau"}  # die 3 nächsten Orte
        assert database.such_anfragen_am(c, date.today().isoformat()) == 0  # nicht gegen das Limit gezählt


def test_normaler_lauf_ohne_key_bekommt_nie_demo_daten(settings, live_settings, monkeypatch, requests_mock):
    # Live-Betrieb an, aber Key fehlt: Fehler statt stiller Demo-Daten
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: live_settings)
    assert main(["web-recherche"]) == 2
    with closing(database.connect(settings)) as c:
        assert database.list_web_funde(c) == []


def test_demo_ohne_db_schreibt_in_demo_datenbank_nicht_in_echte(settings, monkeypatch):
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    assert main(["web-recherche", "--demo"]) == 0
    with closing(database.connect(settings)) as c:
        assert database.list_web_funde(c) == []
    with closing(database.connect(demo_data.demo_settings(settings))) as c:
        assert len(database.list_web_funde(c)) >= 5


def test_demo_verweigert_die_echte_datenbank(settings, monkeypatch):
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    assert main(["web-recherche", "--demo", "--db", settings.database.path]) == 2


def test_demo_verschickt_nie_eine_mail(settings, monkeypatch, tmp_path):
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    aufrufe = []
    monkeypatch.setattr("radar.cli.send_notification", lambda *a, **kw: aufrufe.append(a))
    assert main(["web-recherche", "--demo", "--notify", "--db", str(tmp_path / "t.db")]) == 0
    assert aufrufe == []


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Die Baugenehmigung liegt vor. Baubeginn ist für das Frühjahr geplant.", Phase.BAUGENEHMIGUNG),
        ("Baugenehmigung erteilt, der Baubeginn soll 2027 erfolgen.", Phase.BAUGENEHMIGUNG),
        ("Der geplante Baubeginn verschiebt sich, die Baugenehmigung fehlt noch.", Phase.BAUGENEHMIGUNG),
        ("Baustart ab 2027 vorgesehen.", None),
        ("Mit dem Spatenstich haben die Arbeiten begonnen.", Phase.BAUBEGINN),
        ("Richtfest in Ellerau: Das Wohnprojekt mit Mietwohnungen für Senioren und Familien liegt im Zeitplan, "
         "Bezug ist für Herbst 2027 vorgesehen.", Phase.BAUBEGINN),
        ("Baubeginn war im Mai, der Bezug ist für 2028 geplant und wird vorbereitet.", Phase.BAUBEGINN),
    ],
)
def test_angekuendigter_baubeginn_ist_nicht_phase_4(config, text, erwartet):
    assert klassifiziere_phase(text, config) is erwartet



# -- Eigentumsprojekte (Kundenwunsch 10/2026: ab eigentum_min_we behalten) --------------------------------


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Es entstehen 198 Eigentumswohnungen.", "EIGENTUM"),
        ("96 Mietwohnungen, davon 30 öffentlich gefördert.", "MIETE"),
        ("40 Eigentumswohnungen und 60 geförderte Mietwohnungen im Drittelmix.", "GEMISCHT"),
        ("Neubau mit 50 Wohnungen.", "UNKLAR"),
        ("Neue ETW am Stadtpark", "EIGENTUM"),
        ("Das Netwerk-Treffen", "UNKLAR"),  # "ETW" nur als Wortanfang
    ],
)
def test_bestimme_wohnform(config, text, erwartet):
    from radar.web_recherche import bestimme_wohnform

    assert bestimme_wohnform(text, config) == erwartet


@pytest.mark.parametrize(
    "we, behalten",
    [("198", True), ("12", True), ("11", False), (None, True)],
)
def test_reines_etw_projekt_nur_ab_mindestgroesse(config, we, behalten):
    menge = f"{we} " if we else ""
    ergebnis = _ergebnis(titel=f"Quickborn: Baugenehmigung für {menge}Eigentumswohnungen erteilt")
    fund = bewerte_ergebnis(ergebnis, _auftrag(), config)
    assert (fund is not None) is behalten
    if fund is not None:
        assert fund.wohnform == "EIGENTUM"


def test_kleines_mietprojekt_bleibt_drin(config):
    # Die Mindestgröße gilt nur für reine Eigentumsprojekte, Miet-/Förderprojekte sind nicht betroffen
    ergebnis = _ergebnis(titel="Quickborn: Baugenehmigung für 8 Mietwohnungen erteilt")
    assert bewerte_ergebnis(ergebnis, _auftrag(), config).wohnform == "MIETE"


def test_nordlicht_ist_verifiziertes_phase_4_highlight(config):
    from radar.web_recherche import demo_funde

    nordlicht = next(f for f in demo_funde(config) if f.url == NORDLICHT_URL)
    assert nordlicht.ort == "Norderstedt"
    assert nordlicht.phase is Phase.BAUBEGINN
    assert nordlicht.we_anzahl == 198
    assert nordlicht.wohnform == "EIGENTUM"
    assert "(Demo)" not in nordlicht.titel
    assert "entwicklungsstadt.de" in nordlicht.auszug  # zweite Quelle mitgeführt


def test_wohnform_wird_gespeichert_und_nicht_auf_unklar_zurueckgesetzt(conn):
    database.upsert_web_fund(conn, _fund(wohnform="EIGENTUM"))
    database.upsert_web_fund(conn, _fund(wohnform="UNKLAR"))
    assert database.list_web_funde(conn)[0]["wohnform"] == "EIGENTUM"


def test_dashboard_markiert_herkunft_und_filtert_wohnform(conn):
    database.upsert_web_fund(conn, _fund(url=NORDLICHT_URL, wohnform="EIGENTUM"))
    database.upsert_web_fund(conn, _fund(url="https://example.org/demo/x", wohnform="MIETE"))
    database.upsert_web_fund(conn, _fund(url="https://www.abendblatt.de/echt", wohnform="UNKLAR"))
    df = build_web_funde_df(database.list_web_funde(conn)).set_index("url")
    assert df.loc[NORDLICHT_URL, "hinweis"].startswith("⭐")
    assert df.loc["https://example.org/demo/x", "hinweis"] == "Demo (erfunden)"
    assert df.loc["https://www.abendblatt.de/echt", "hinweis"] == ""
    assert list(apply_web_filter(df.reset_index(), wohnformen=["EIGENTUM"])["url"]) == [NORDLICHT_URL]


def test_verifizierte_urls_sind_bereits_normalisiert():
    # sonst passt die gespeicherte (normalisierte) URL nicht mehr zur Markierung im Dashboard
    assert all(normalisiere_url(u) == u for u in VERIFIZIERTE_DEMO_URLS)



def test_wiederholte_demo_zeigt_alle_funde_im_terminal(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    befehl = ["web-recherche", "--demo", "--test", "--db", str(tmp_path / "t.db")]
    assert main(befehl) == 0
    capsys.readouterr()
    assert main(befehl) == 0  # zweiter Lauf: nichts mehr neu
    ausgabe = capsys.readouterr().out
    assert "Beispielweg" in ausgabe and "Nordlicht" in ausgabe
    assert "NEU |" not in ausgabe


def test_web_recherche_nutzt_dieselbe_etw_grenze_wie_ratsunterlagen(settings):
    strenger = replace(settings, filters=replace(settings.filters, eigentum_min_we=50))
    assert load_web_config(strenger).eigentum_min_we == 50


def test_etw_grenze_in_web_yaml_wird_abgelehnt(settings, tmp_path):
    pfad = tmp_path / "web.yaml"
    pfad.write_text("eigentum_min_we: 12\n", encoding="utf-8")
    with pytest.raises(ValueError, match="settings.yaml"):
        load_web_config(settings, pfad)



# -- Live-Betrieb aus (Standard): nie eine Anfrage an Tavily ------------------------------------------


def test_echte_konfiguration_hat_live_betrieb_aus():
    from radar.config import load_settings

    assert load_settings().betrieb.live_api_aufrufe is False


@pytest.mark.parametrize("befehl", [["web-recherche"], ["web-recherche", "--test"], ["web-recherche", "--notify"]])
def test_ohne_live_betrieb_laeuft_jeder_cli_aufruf_als_demo(settings, monkeypatch, requests_mock, capsys, befehl):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-vorhanden-aber-ohne-guthaben")
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    gesendet = []
    monkeypatch.setattr("radar.cli.send_notification", lambda *a, **kw: gesendet.append(a))

    assert main(befehl) == 0

    assert not requests_mock.called  # keine Tavily-Anfrage, obwohl ein Key gesetzt ist
    assert gesendet == []  # Demo-Funde werden nie gemailt
    assert "DEMO-MODUS" in capsys.readouterr().out
    with closing(database.connect(settings)) as c:
        assert database.list_web_funde(c) == []  # echte Datenbank unberührt


def test_run_web_recherche_verweigert_echte_suche_ohne_live_betrieb(settings, monkeypatch, requests_mock):
    from radar.web_recherche import LiveAufrufeDeaktiviert, run_web_recherche

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    with pytest.raises(LiveAufrufeDeaktiviert):
        run_web_recherche(settings)
    assert not requests_mock.called


# -- Echte Projekte in der Frühphase (SonnenHeyde, Richtweg): werden verworfen ----------------------------


def test_echte_fruehphasen_projekte_werden_von_der_bewertung_verworfen(config):
    from radar.web_recherche import (
        DEMO_SUCHERGEBNISSE,
        GRUND_FRUEHPHASE,
        RICHTWEG_URL,
        SONNENHEYDE_URL,
        demo_funde,
        pruefe_ergebnis,
    )

    norderstedt = next(o for o, _ in orte_im_radius(config) if o.name == "Norderstedt")
    auftrag = Suchauftrag(ort=norderstedt, entfernung_km=0.0, phase=Phase.BAUGENEHMIGUNG, anfrage="q")
    for url in (SONNENHEYDE_URL, RICHTWEG_URL):
        ergebnis = next(e for e in DEMO_SUCHERGEBNISSE[("Norderstedt", Phase.BAUGENEHMIGUNG)] if e.url == url)
        fund, grund = pruefe_ergebnis(ergebnis, auftrag, config, heute=date(2026, 10, 29))
        assert fund is None and grund == GRUND_FRUEHPHASE
    assert not {SONNENHEYDE_URL, RICHTWEG_URL} & {f.url for f in demo_funde(config)}


@pytest.mark.parametrize("jahr, erwartet", [(2026, None), (2028, Phase.BAUBEGINN)])
def test_baubeginn_in_kuenftigem_jahr_ist_nur_angekuendigt(config, jahr, erwartet):
    # Ohne "geplant"/"soll": allein das künftige Jahr macht es zur Ankündigung
    text = "Den Baubeginn setzt der Entwickler für 2027 an."
    assert klassifiziere_phase(text, config, heute=date(jahr, 1, 1)) is erwartet


def test_demo_terminal_zeigt_verworfene_echte_projekte(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("radar.web_recherche.load_settings", lambda: settings)
    test_db = tmp_path / "t.db"
    assert main(["web-recherche", "--demo", "--test", "--db", str(test_db)]) == 0
    ausgabe = capsys.readouterr().out
    assert ausgabe.count("⭐ ECHTES PROJEKT (In Frühphase verworfen)") == 2
    assert "SonnenHeyde (Manke)" in ausgabe and "Richtweg (Instone)" in ausgabe
    assert "⭐ ECHTES PROJEKT (Quelle geprüft)" in ausgabe  # Nordlicht weiterhin als Treffer
    with closing(database.connect(replace(settings, database=DatabaseConfig(path=str(test_db))))) as c:
        urls = {f["url"] for f in database.list_web_funde(c)}
    assert "manke-bau.de" not in " ".join(urls) and "erwirbt-grundstueck" not in " ".join(urls)


def test_nur_belegte_echte_projekte_in_der_demo():
    from radar.web_recherche import DEMO_SUCHERGEBNISSE

    echte = {e.url for liste in DEMO_SUCHERGEBNISSE.values() for e in liste if "example.org" not in e.url}
    echte -= {"https://www.immobilienscout24.de/expose/demo-0000"}  # erfundenes Portal-Negativbeispiel
    assert {normalisiere_url(u) for u in echte} == VERIFIZIERTE_DEMO_URLS
    assert not any(w in " ".join(echte) for w in ("vonovia", "buwog", "noa4", "adlershorst"))
