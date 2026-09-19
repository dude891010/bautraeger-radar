"""Tests für den Hamburg-Transparenzportal-Adapter (radar/sources/hamburg_transparenz.py) gegen
echte, gespeicherte CKAN-Antworten (tests/fixtures/hamburg_transparenz/). Kein Netzzugriff:
`requests_mock` ersetzt den Transport. Die Fixtures wurden am 19.09.2026 einmalig live abgerufen."""

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

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
from radar.sources.base import Sitzung, SourceLayoutError
from radar.sources.hamburg_transparenz import HamburgTransparenzAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "hamburg_transparenz"
API_HOST = "suche.transparenz.hamburg.de"
BASE_URL = f"https://{API_HOST}/api/3/action/"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
        id="hamburg_transparenz",
        name="Transparenzportal Hamburg (Bauleitpläne)",
        bundesland="HH",
        system="hamburg_transparenz",
        base_url=BASE_URL,
        status="verified",
        enabled=False,
        additional_hosts=["daten-hamburg.de", "archiv.transparenz.hamburg.de"],
        ignore_robots_txt=True,
        notes="Testquelle, siehe CLAUDE.md Referenzabschnitt.",
    )


@pytest.fixture
def adapter(source, settings, requests_mock):
    # ignore_robots_txt=True -> kein robots.txt-Abruf für den primären Host nötig/erwartet.
    http = HttpClient(source, settings)
    return HamburgTransparenzAdapter(source, http)


def _mock_search(requests_mock, *, rows=100, start_row=0, payload=None):
    payload = payload if payload is not None else _load("search_bauleitplaene_2026.json")
    fq = (
        "registerobject_type:bauleitplaene AND "
        "publishing_date:[2026-06-01T00:00:00Z TO 2026-12-31T23:59:59Z]"
    )
    query = urlencode({"q": "*:*", "fq": fq, "rows": rows, "start": start_row})
    requests_mock.get(f"{BASE_URL}package_search?{query}", json=payload)


# -- list_sessions ------------------------------------------------------------------------------


def test_list_sessions_maps_each_package_to_a_session(requests_mock, adapter):
    _mock_search(requests_mock)
    sessions = adapter.list_sessions(date(2026, 6, 1), date(2026, 12, 31))

    assert len(sessions) == 18  # 'count' der echten Fixture
    alsterdorf = next(s for s in sessions if s.external_id == "bebauungsplan-alsterdorf-8-2-anderung-hamburg")
    assert alsterdorf.gremium == "Bauleitplanung"
    assert alsterdorf.datum == date(2026, 7, 27)  # publishing_date aus der Fixture
    assert alsterdorf.url == f"{BASE_URL}package_show?id=bebauungsplan-alsterdorf-8-2-anderung-hamburg"
    assert alsterdorf.abgesagt is False


def test_list_sessions_paginates_over_the_real_result_set(requests_mock, adapter, monkeypatch):
    """Mit einer künstlich kleinen Seitengröße muss der Adapter über mehrere Seiten der ECHTEN
    18 Treffer iterieren und alle einsammeln, nicht nur die erste Seite."""
    import radar.sources.hamburg_transparenz as module

    monkeypatch.setattr(module, "PAGE_SIZE", 5)
    full = _load("search_bauleitplaene_2026.json")
    all_results = full["result"]["results"]
    count = full["result"]["count"]
    assert count == len(all_results) == 18

    for offset in range(0, count, 5):
        page = deepcopy(full)
        page["result"]["results"] = all_results[offset : offset + 5]
        _mock_search(requests_mock, rows=5, start_row=offset, payload=page)

    sessions = adapter.list_sessions(date(2026, 6, 1), date(2026, 12, 31))

    assert len(sessions) == 18
    assert {s.external_id for s in sessions} == {pkg["name"] for pkg in all_results}


def test_list_sessions_rejects_search_error_response(requests_mock, adapter):
    _mock_search(requests_mock, payload={"success": False, "error": {"message": "kaputt"}})
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 6, 1), date(2026, 12, 31))


def test_list_sessions_skips_package_without_publishing_date(requests_mock, adapter):
    payload = {
        "success": True,
        "result": {"count": 1, "results": [{"name": "ohne-datum", "extras": []}]},
    }
    _mock_search(requests_mock, payload=payload)
    sessions = adapter.list_sessions(date(2026, 6, 1), date(2026, 12, 31))
    assert sessions == []


# -- list_documents -------------------------------------------------------------------------------


def test_list_documents_returns_empty_when_no_url(adapter):
    sitzung = Sitzung(source_id="hamburg_transparenz", external_id="x", gremium="G", datum=date(2026, 1, 1), url=None)
    assert adapter.list_documents(sitzung) == []


def test_list_documents_keeps_only_pdf_resources_and_groups_under_one_vorlage(requests_mock, adapter):
    """Reales Package mit GML-, drei PDF- und einer HTML-Ressource: nur die PDFs werden zu
    Dokumenten, alle unter derselben (synthetischen) Vorlage wie die Sitzung selbst - ein Package
    ist hier zugleich Sitzung UND einzige Vorlage."""
    package_url = f"{BASE_URL}package_show?id=bebauungsplan-alsterdorf-8-2-anderung-hamburg"
    requests_mock.get(package_url, json=_load("package_alsterdorf8.json"))
    sitzung = Sitzung(
        source_id="hamburg_transparenz",
        external_id="bebauungsplan-alsterdorf-8-2-anderung-hamburg",
        gremium="Bauleitplanung",
        datum=date(2026, 7, 27),
        url=package_url,
    )

    documents = adapter.list_documents(sitzung)

    assert len(documents) == 3  # GML und HTML fallen raus
    assert all(d.vorlage_external_id == sitzung.external_id for d in documents)
    assert all(d.sitzung_external_id == sitzung.external_id for d in documents)
    assert all(d.url.startswith("http://daten-hamburg.de/") for d in documents)

    by_typ = {d.typ: d for d in documents}
    assert by_typ["begruendung"].titel == "Begründung des Bebauungsplans als PDF Datei"
    assert "bekanntmachung" in by_typ
    assert "festsetzung" in by_typ


def test_list_documents_rejects_package_show_error_response(requests_mock, adapter):
    package_url = f"{BASE_URL}package_show?id=kaputt"
    requests_mock.get(package_url, json={"success": False})
    sitzung = Sitzung(
        source_id="hamburg_transparenz", external_id="kaputt", gremium="Bauleitplanung", datum=date(2026, 1, 1),
        url=package_url,
    )
    with pytest.raises(SourceLayoutError):
        adapter.list_documents(sitzung)
