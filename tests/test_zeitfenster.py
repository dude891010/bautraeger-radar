"""Zeitfenster je Quelle (Hummel-Abgleich 11/2026): Backfill ab `scan.backfill_ab`, Überlappung von
mindestens `scan.ueberlappung_wochen`, Sitzungen ohne Detailseite/Niederschrift erneut prüfen,
weitere Gremien für Norderstedt und Warnung im Dashboard bei zu altem letzten Lauf. Ohne Netz."""

from datetime import UTC, date, datetime, timedelta

import pytest

from radar import database
from radar.committees import committee_matches
from radar.config import (
    BetriebConfig,
    DashboardConfig,
    DatabaseConfig,
    FiltersConfig,
    LLMConfig,
    ScanConfig,
    ScraperConfig,
    Settings,
    Source,
    load_settings,
    load_sources,
)
from radar.scraper import SourceRunResult, run_full, scan_range_fuer_quelle
from radar.sources.base import Dokument, Sitzung
from tests.conftest import FakeAnthropicClient

HEUTE = date(2026, 11, 14)
ASV = "Ausschuss für Stadtentwicklung und Verkehr"


def _settings(tmp_path, **scan):
    return Settings(
        scan=ScanConfig(**scan),
        scraper=ScraperConfig(request_delay_seconds=0.0),
        filters=FiltersConfig(default_committee_patterns=["stadtentwicklung"], prefilter_keywords=["Wohneinheit"]),
        llm=LLMConfig(cache_dir=str(tmp_path / "llm_cache")),
        database=DatabaseConfig(path=str(tmp_path / "radar.db")),
        dashboard=DashboardConfig(),
        betrieb=BetriebConfig(live_api_aufrufe=False),
    )


@pytest.fixture
def source():
    return Source(
        id="test", name="Stadt Test", bundesland="SH", system="sessionnet", base_url="https://example.org/",
        status="verified", enabled=True, tier="A",
    )


@pytest.fixture
def conn(tmp_path, monkeypatch, source):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    connection = database.connect(_settings(tmp_path))
    database.ensure_quelle(connection, source)
    yield connection
    connection.close()


def _erfolg(conn, source, wann: date):
    database.record_source_run(
        conn, source, SourceRunResult(source_id=source.id), when=datetime(wann.year, wann.month, wann.day, tzinfo=UTC)
    )


def _sitzung(external_id, datum, url=None, gremium=ASV):
    return Sitzung(source_id="test", external_id=external_id, gremium=gremium, datum=datum, url=url)


# -- Konfiguration --------------------------------------------------------------------------------


def test_scan_config_liest_backfill_als_text_oder_datum():
    assert ScanConfig(backfill_ab="2024-01-01").backfill_ab == date(2024, 1, 1)
    assert ScanConfig(backfill_ab=date(2024, 1, 1)).backfill_ab == date(2024, 1, 1)
    assert ScanConfig().backfill_ab is None
    with pytest.raises(ValueError):
        ScanConfig(ueberlappung_wochen=0)


def test_echte_konfiguration_backfill_ab_2024_und_sechs_wochen():
    scan = load_settings().scan
    assert scan.backfill_ab == date(2024, 1, 1)
    assert scan.ueberlappung_wochen >= 6


def test_norderstedt_liest_asv_hauptausschuss_und_stadtvertretung():
    settings = load_settings()
    norderstedt = next(s for s in load_sources(settings) if s.id == "norderstedt")
    muster = settings.committee_patterns_for(norderstedt)
    for gremium in (ASV, "Hauptausschuss", "Stadtvertretung"):
        assert committee_matches(gremium, muster), gremium
    for gremium in ("Kulturausschuss", "Kinder- und Jugendbeirat", "Sozialausschuss"):
        assert not committee_matches(gremium, muster), gremium


# -- Fenster je Quelle ----------------------------------------------------------------------------


def test_backfill_solange_quelle_nicht_ab_backfill_datum_erfasst(conn, source, tmp_path):
    settings = _settings(tmp_path, backfill_ab=date(2024, 1, 1))
    _erfolg(conn, source, date(2026, 9, 19))  # ein Lauf mit kurzem Fenster zählt nicht als Backfill
    start, end = scan_range_fuer_quelle(conn, source, settings, today=HEUTE)
    assert start == date(2024, 1, 1)
    assert end == date(2027, 1, 14)


def test_nach_backfill_letzter_erfolg_minus_ueberlappung(conn, source, tmp_path):
    settings = _settings(tmp_path, backfill_ab=date(2024, 1, 1), ueberlappung_wochen=6)
    database.markiere_erfasst_ab(conn, source.id, date(2024, 1, 1))
    _erfolg(conn, source, date(2026, 11, 10))
    start, _ = scan_range_fuer_quelle(conn, source, settings, today=HEUTE)
    assert start == date(2026, 11, 10) - timedelta(weeks=6)  # früher als HEUTE - 6 Wochen


def test_randtag_geht_nicht_verloren(conn, source, tmp_path):
    """Fall Bahnhofstraße/Schweriner Straße: ASV am 18.06., Fenster begann am 19.06."""
    settings = _settings(tmp_path, ueberlappung_wochen=6)
    database.markiere_erfasst_ab(conn, source.id, date(2026, 6, 19))
    _erfolg(conn, source, date(2026, 6, 19))
    start, _ = scan_range_fuer_quelle(conn, source, settings, today=date(2026, 6, 26))
    assert start <= date(2026, 6, 18)


def test_mindestens_ueberlappung_vor_heute(conn, source, tmp_path):
    settings = _settings(tmp_path, ueberlappung_wochen=6)
    _erfolg(conn, source, HEUTE)  # Lauf von heute: trotzdem 6 Wochen zurück
    start, _ = scan_range_fuer_quelle(conn, source, settings, today=HEUTE)
    assert start == HEUTE - timedelta(weeks=6)


def test_ohne_backfill_und_ohne_erfolg_months_back(conn, source, tmp_path):
    settings = _settings(tmp_path, months_back=3)
    start, _ = scan_range_fuer_quelle(conn, source, settings, today=HEUTE)
    assert start == date(2026, 8, 14)


def test_offene_sitzung_ohne_detailseite_wird_erneut_geprueft(conn, source, tmp_path):
    settings = _settings(tmp_path, ueberlappung_wochen=6, offene_sitzungen_max_monate=6)
    _erfolg(conn, source, HEUTE)
    database.upsert_sitzung(conn, "test", _sitzung("2026-08-01-asv", date(2026, 8, 1)))  # ohne url
    start, _ = scan_range_fuer_quelle(conn, source, settings, today=HEUTE)
    assert start == date(2026, 8, 1)


def test_offene_sitzung_ohne_niederschrift_bis_sie_da_ist(conn, source, tmp_path):
    settings = _settings(tmp_path, ueberlappung_wochen=6, offene_sitzungen_max_monate=6)
    _erfolg(conn, source, HEUTE)
    sitzung_id = database.upsert_sitzung(conn, "test", _sitzung("100", date(2026, 9, 3), url="si0057.php?__ksinr=100"))
    assert scan_range_fuer_quelle(conn, source, settings, today=HEUTE)[0] == date(2026, 9, 3)

    niederschrift = Dokument(
        source_id="test", external_id="n1", titel="Niederschrift", url="getfile.php?id=n1",
        sitzung_external_id="100", typ="niederschrift",
    )
    database.upsert_dokument(conn, "test", sitzung_id, None, niederschrift)
    assert scan_range_fuer_quelle(conn, source, settings, today=HEUTE)[0] == HEUTE - timedelta(weeks=6)


def test_offene_sitzung_aelter_als_grenze_verlaengert_nicht(conn, source, tmp_path):
    settings = _settings(tmp_path, ueberlappung_wochen=6, offene_sitzungen_max_monate=6)
    _erfolg(conn, source, HEUTE)
    database.upsert_sitzung(conn, "test", _sitzung("alt", date(2025, 1, 10)))
    assert scan_range_fuer_quelle(conn, source, settings, today=HEUTE)[0] == HEUTE - timedelta(weeks=6)


# -- Datenbank ------------------------------------------------------------------------------------


def test_markiere_erfasst_ab_behaelt_das_fruehere_datum(conn, source):
    database.markiere_erfasst_ab(conn, source.id, date(2024, 1, 1))
    database.markiere_erfasst_ab(conn, source.id, date(2026, 10, 1))  # Wochenlauf vergisst Backfill nicht
    assert database.quellen_zeitraum(conn, source.id)[0] == date(2024, 1, 1)


def test_platzhalter_sitzung_wird_bei_echter_detailseite_uebernommen(conn):
    platzhalter = database.upsert_sitzung(conn, "test", _sitzung("2026-10-01-asv", date(2026, 10, 1)))
    echt = database.upsert_sitzung(conn, "test", _sitzung("17400", date(2026, 10, 1), url="si0057.php?__ksinr=17400"))
    assert echt == platzhalter
    zeilen = conn.execute("SELECT external_id, url FROM sitzungen").fetchall()
    assert [(z["external_id"], z["url"]) for z in zeilen] == [("17400", "si0057.php?__ksinr=17400")]


def test_platzhalter_anderes_gremium_bleibt_getrennt(conn):
    database.upsert_sitzung(conn, "test", _sitzung("2026-10-01-ha", date(2026, 10, 1), gremium="Hauptausschuss"))
    database.upsert_sitzung(conn, "test", _sitzung("17400", date(2026, 10, 1), url="si0057.php?__ksinr=17400"))
    assert conn.execute("SELECT COUNT(*) FROM sitzungen").fetchone()[0] == 2


def test_letzter_lauf_gesamt(conn, source):
    assert database.letzter_lauf_gesamt(conn) is None
    _erfolg(conn, source, date(2026, 9, 19))
    assert database.letzter_lauf_gesamt(conn).date() == date(2026, 9, 19)


# -- vollständiger Lauf markiert den erfassten Zeitraum ---------------------------------------------


class _Adapter:
    def __init__(self, fehler=False):
        self.fenster = None
        self._fehler = fehler

    def list_sessions(self, start, end):
        self.fenster = (start, end)
        if self._fehler:
            raise RuntimeError("Kalender nicht ladbar")
        return []

    def list_documents(self, sitzung):
        return []


@pytest.mark.parametrize("kalender_kaputt", [False, True])
def test_run_full_nutzt_backfill_und_merkt_erfasst_ab(monkeypatch, tmp_path, source, kalender_kaputt):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    settings = _settings(tmp_path, backfill_ab=date(2024, 1, 1))
    adapter = _Adapter(fehler=kalender_kaputt)
    monkeypatch.setattr("radar.scraper.load_sources", lambda s: [source])
    monkeypatch.setattr("radar.scraper.HttpClient", lambda src, s: object())
    monkeypatch.setattr("radar.scraper.get_adapter", lambda src, http: adapter)

    run_full(settings, client=FakeAnthropicClient([]))

    assert adapter.fenster[0] == date(2024, 1, 1)
    conn = database.connect(settings)
    try:
        erfasst_ab, _ = database.quellen_zeitraum(conn, source.id)
    finally:
        conn.close()
    # Kalender abgebrochen -> kein erfasst_ab, der nächste Lauf holt den Backfill erneut.
    assert erfasst_ab == (None if kalender_kaputt else date(2024, 1, 1))


# -- Dashboard ------------------------------------------------------------------------------------


def test_lauf_hinweis_warnt_nach_14_tagen():
    from app import lauf_hinweis

    jetzt = datetime(2026, 11, 14, 12, tzinfo=UTC)
    art, text = lauf_hinweis(datetime(2026, 9, 19, 19, 45, tzinfo=UTC), jetzt)
    assert art == "warning"
    assert "19.09.2026 21:45" in text  # Ortszeit Berlin
    assert "55 Tagen" in text  # volle Tage

    art, text = lauf_hinweis(datetime(2026, 11, 10, tzinfo=UTC), jetzt)
    assert art == "info"
    assert "vor 4 Tagen" in text

    assert lauf_hinweis(None, jetzt)[0] == "warning"


# -- Backfill in Scheiben (--von/--bis) ------------------------------------------------------------


def test_scheibe_ohne_anschluss_setzt_erfasst_ab_nicht(monkeypatch, tmp_path, source):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    settings = _settings(tmp_path, backfill_ab=date(2024, 1, 1))
    adapter = _Adapter()
    monkeypatch.setattr("radar.scraper.load_sources", lambda s: [source])
    monkeypatch.setattr("radar.scraper.HttpClient", lambda src, s: object())
    monkeypatch.setattr("radar.scraper.get_adapter", lambda src, http: adapter)

    def erfasst_ab():
        c = database.connect(settings)
        try:
            return database.quellen_zeitraum(c, source.id)[0]
        finally:
            c.close()

    # 2025 allein: noch nichts erfasst und nicht bis heute -> Lücke, nicht merken
    run_full(settings, client=FakeAnthropicClient([]), zeitraum=(date(2025, 1, 1), date(2025, 12, 31)))
    assert adapter.fenster == (date(2025, 1, 1), date(2025, 12, 31))
    assert erfasst_ab() is None

    # neueste Scheibe bis heute -> erfasst_ab gesetzt
    run_full(settings, client=FakeAnthropicClient([]), zeitraum=(date(2026, 1, 1), None))
    assert erfasst_ab() == date(2026, 1, 1)

    # lückenlos davor -> rückt vor; mit Lücke (2023 statt 2025) -> bleibt
    run_full(settings, client=FakeAnthropicClient([]), zeitraum=(date(2024, 1, 1), date(2025, 12, 31)))
    assert erfasst_ab() == date(2024, 1, 1)
    run_full(settings, client=FakeAnthropicClient([]), zeitraum=(date(2022, 1, 1), date(2022, 12, 31)))
    assert erfasst_ab() == date(2024, 1, 1)


def test_cli_scrape_von_bis(monkeypatch):
    from radar.cli import main

    gesehen = {}
    monkeypatch.setattr("radar.cli.run_full", lambda **kw: gesehen.update(kw) or [])
    assert main(["scrape", "--von", "2025-01-01", "--bis", "2025-12-31"]) == 0
    assert gesehen["zeitraum"] == (date(2025, 1, 1), date(2025, 12, 31))
    with pytest.raises(SystemExit):
        main(["scrape", "--bis", "2025-12-31"])


def test_cli_scrape_nur_eine_quelle(monkeypatch):
    from radar.cli import main

    gesehen = {}
    monkeypatch.setattr("radar.cli.run_full", lambda **kw: gesehen.update(kw) or [])
    assert main(["scrape", "--quelle", "norderstedt_presse"]) == 0
    assert gesehen == {"quellen": ["norderstedt_presse"]}


def test_run_full_filtert_quellen_und_lehnt_unbekannte_ab(monkeypatch, tmp_path, source):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    settings = _settings(tmp_path)
    andere = Source(
        id="andere", name="Andere", bundesland="SH", system="sessionnet", base_url="https://example.com/",
        status="verified", enabled=True, tier="A",
    )
    gelaufen = []
    monkeypatch.setattr("radar.scraper.load_sources", lambda s: [source, andere])
    monkeypatch.setattr("radar.scraper.HttpClient", lambda src, s: object())
    monkeypatch.setattr("radar.scraper.get_adapter", lambda src, http: gelaufen.append(src.id) or _Adapter())
    run_full(settings, client=FakeAnthropicClient([]), quellen=["andere"])
    assert gelaufen == ["andere"]
    with pytest.raises(ValueError):
        run_full(settings, client=FakeAnthropicClient([]), quellen=["gibtsnicht"])
