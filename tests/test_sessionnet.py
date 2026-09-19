"""Tests für den SessionNet-Adapter gegen echte, gespeicherte Antworten von Norderstedt
(tests/fixtures/norderstedt/, siehe radar/sources/sessionnet.py für die Herkunft). Kein Netzzugriff:
`requests_mock` ersetzt den Transport, die Fixtures wurden am 19.09.2026 einmalig live abgerufen."""

from datetime import date
from pathlib import Path

import pytest

from radar.config import (
    DashboardConfig,
    DatabaseConfig,
    FiltersConfig,
    LLMConfig,
    ScanConfig,
    ScraperConfig,
    Settings,
    Source,
)
from radar.http_client import HttpClient
from radar.sources.base import SourceLayoutError
from radar.sources.sessionnet import SessionNetAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "norderstedt"
BASE_URL = "https://buergerinfo.norderstedt.de/ratsinfo/sessionnet/buergerinfo/"


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(request_delay_seconds=0.0),
        filters=FiltersConfig(),
        llm=LLMConfig(),
        database=DatabaseConfig(),
        dashboard=DashboardConfig(),
    )


@pytest.fixture
def source():
    return Source(
        id="norderstedt",
        name="Stadt Norderstedt",
        bundesland="SH",
        system="sessionnet",
        base_url=BASE_URL,
        status="verified",
        enabled=True,
        options={"entry_page": "info.php", "calendar_page": "si0040.php"},
    )


@pytest.fixture
def adapter(source, settings, requests_mock):
    # robots.txt wird schon im HttpClient-Konstruktor abgerufen, muss also vor dessen Erzeugung
    # gemockt sein (nicht erst im Testkörper wie die übrigen, erst später gebrauchten Mocks).
    requests_mock.get("https://buergerinfo.norderstedt.de/robots.txt", status_code=404)
    http = HttpClient(source, settings)
    return SessionNetAdapter(source, http)


def _mock_common(requests_mock):
    requests_mock.get("https://buergerinfo.norderstedt.de/robots.txt", status_code=404)
    requests_mock.get(BASE_URL + "info.php", text=(FIXTURES / "entry.html").read_text(encoding="utf-8"))


def _mock_september_calendar(requests_mock):
    requests_mock.get(
        BASE_URL + "si0040.php?__cjahr=2026&__cmonat=9&__canz=1&__cselect=0",
        text=(FIXTURES / "kalender_2026_09.html").read_text(encoding="utf-8"),
    )


def _mock_asv_session(requests_mock):
    requests_mock.get(
        BASE_URL + "si0057.php?__ksinr=16868",
        text=(FIXTURES / "sitzung_16868_asv.html").read_text(encoding="utf-8"),
    )


# -- list_sessions ------------------------------------------------------------------------------


def test_list_sessions_finds_asv_session_with_detail_link(requests_mock, adapter):
    _mock_common(requests_mock)
    _mock_september_calendar(requests_mock)

    sessions = adapter.list_sessions(date(2026, 9, 1), date(2026, 9, 30))
    asv = next(s for s in sessions if s.external_id == "16868")

    assert asv.gremium == "Ausschuss für Stadtentwicklung und Verkehr"
    assert asv.datum == date(2026, 9, 3)
    assert asv.uhrzeit == "18:30-20:03 Uhr"
    assert asv.ort is not None and "Rathausallee 50" in asv.ort
    assert asv.url == "si0057.php?__ksinr=16868"
    assert asv.abgesagt is False


def test_list_sessions_marks_cancelled_session(requests_mock, adapter):
    _mock_common(requests_mock)
    _mock_september_calendar(requests_mock)

    sessions = adapter.list_sessions(date(2026, 9, 1), date(2026, 9, 30))
    cancelled = next(s for s in sessions if s.datum == date(2026, 9, 9))

    assert cancelled.abgesagt is True
    assert cancelled.url is None
    assert "Stadtwerkeausschus" in cancelled.gremium  # Genitivform, siehe sessionnet.py


def test_list_sessions_handles_session_without_detail_page(requests_mock, adapter):
    """Nicht jede Sitzung hat eine Detailseite (hier: Kinder- und Jugendbeirat am 04.09.2026).
    Das ist laut CLAUDE.md ein Normalfall, kein Fehler."""
    _mock_common(requests_mock)
    _mock_september_calendar(requests_mock)

    sessions = adapter.list_sessions(date(2026, 9, 1), date(2026, 9, 30))
    beirat = next(s for s in sessions if s.gremium == "Kinder- und Jugendbeirat" and s.datum == date(2026, 9, 4))

    assert beirat.url is None
    assert beirat.abgesagt is False
    assert beirat.external_id  # synthetische ID vorhanden, auch ohne __ksinr


def test_list_sessions_skips_days_without_any_session(requests_mock, adapter):
    _mock_common(requests_mock)
    _mock_september_calendar(requests_mock)

    sessions = adapter.list_sessions(date(2026, 9, 1), date(2026, 9, 30))
    assert all(s.datum != date(2026, 9, 1) for s in sessions)  # 1.9. ist ein leerer Tag


def test_list_sessions_filters_by_date_range(requests_mock, adapter):
    _mock_common(requests_mock)
    _mock_september_calendar(requests_mock)

    sessions = adapter.list_sessions(date(2026, 9, 1), date(2026, 9, 5))
    assert all(date(2026, 9, 1) <= s.datum <= date(2026, 9, 5) for s in sessions)
    assert any(s.external_id == "16868" for s in sessions)  # 03.09. liegt im Bereich


def test_list_sessions_rejects_page_without_sessionnet_fingerprint(requests_mock, adapter):
    _mock_common(requests_mock)
    requests_mock.get(
        BASE_URL + "si0040.php?__cjahr=2026&__cmonat=9&__canz=1&__cselect=0",
        text="<html><head><title>Andere Software</title></head><body>kein SessionNet</body></html>",
    )
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 9, 1), date(2026, 9, 30))


def test_list_sessions_rejects_missing_calendar_table(requests_mock, adapter):
    _mock_common(requests_mock)
    requests_mock.get(
        BASE_URL + "si0040.php?__cjahr=2026&__cmonat=9&__canz=1&__cselect=0",
        text='<html><head><meta name="sessionnet" content="V:1"/></head><body>kein Kalender</body></html>',
    )
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 9, 1), date(2026, 9, 30))


# -- list_documents -------------------------------------------------------------------------------


def test_list_documents_returns_none_when_no_detail_page(requests_mock, source, settings):
    from radar.sources.base import Sitzung

    requests_mock.get("https://buergerinfo.norderstedt.de/robots.txt", status_code=404)
    adapter = SessionNetAdapter(source, HttpClient(source, settings))
    sitzung = Sitzung(source_id="norderstedt", external_id="x", gremium="G", datum=date(2026, 9, 4), url=None)
    assert adapter.list_documents(sitzung) == []


def test_list_documents_collects_session_and_top_level_documents(requests_mock, adapter):
    from radar.sources.base import Sitzung

    _mock_common(requests_mock)
    _mock_asv_session(requests_mock)

    sitzung = Sitzung(
        source_id="norderstedt",
        external_id="16868",
        gremium="Ausschuss für Stadtentwicklung und Verkehr",
        datum=date(2026, 9, 3),
        url="si0057.php?__ksinr=16868",
    )
    documents = adapter.list_documents(sitzung)
    by_id = {d.external_id: d for d in documents}

    assert len(documents) == 36
    assert len(by_id) == 36  # keine Duplikate (Icon- und Text-Link verweisen auf dasselbe Dokument)

    bekanntmachung = by_id["223446"]
    assert bekanntmachung.titel == "Öffentliche Bekanntmachung"
    assert bekanntmachung.typ == "bekanntmachung"
    assert bekanntmachung.vorlage_external_id is None

    niederschrift = by_id["223757"]
    assert niederschrift.typ == "niederschrift"

    vorlage_doc = by_id["223408"]
    assert vorlage_doc.typ == "vorlage"
    assert vorlage_doc.vorlage_external_id == "20314"  # Vorlage A 26/0338
    assert vorlage_doc.vorlage_nr == "A 26/0338"

    anlage_doc = by_id["223477"]
    assert anlage_doc.typ == "anlage"
    assert anlage_doc.vorlage_external_id == "20314"
    assert anlage_doc.vorlage_nr == "A 26/0338"
    assert "Anlage" in anlage_doc.titel

    assert bekanntmachung.vorlage_nr is None  # sitzungsweites Dokument, keiner Vorlage zugeordnet

    for doc in documents:
        assert doc.source_id == "norderstedt"
        assert doc.sitzung_external_id == "16868"
        assert doc.url.startswith("getfile.php?id=")


def test_list_documents_excludes_nonpublic_agenda_items(requests_mock, adapter):
    """CLAUDE.md: nur öffentliche Sitzungsunterlagen. Simuliert einen nichtöffentlichen TOP
    (Badge "N 1" statt "Ö ..."), dessen Dokument nicht erfasst werden darf."""
    from radar.sources.base import Sitzung

    html = (FIXTURES / "sitzung_16868_asv.html").read_text(encoding="utf-8")
    # Einen klar erkennbaren nichtöffentlichen TOP mit eigenem Dokument einfügen.
    injected = html.replace(
        "</tbody>",
        '<tr class="smc-t-r-l"><td class="tofnum"><span class="badge">N 1</span></td>'
        '<td class="tolink">Nichtöffentlicher Punkt</td><td class="toxx"></td>'
        '<td class="smc-t-cl991 sidocs"><div class="smc-d-el">'
        '<a class="smce-a-u" href="getfile.php?id=999999&amp;type=do">Geheimes Dokument</a>'
        "</div></td></tr></tbody>",
        1,
    )
    _mock_common(requests_mock)
    requests_mock.get(BASE_URL + "si0057.php?__ksinr=16868", text=injected)

    sitzung = Sitzung(
        source_id="norderstedt",
        external_id="16868",
        gremium="ASV",
        datum=date(2026, 9, 3),
        url="si0057.php?__ksinr=16868",
    )
    documents = adapter.list_documents(sitzung)
    assert not any(d.external_id == "999999" for d in documents)
