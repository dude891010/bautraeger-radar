"""Tests für den OParl-Adapter gegen echte, gespeicherte Antworten der Gemeinde Uplengen
(tests/fixtures/uplengen/, STERNBERG SD.NET RIM, siehe radar/sources/oparl.py für die Herkunft).
Kein Netzzugriff: `requests_mock` ersetzt den Transport. Die Fixtures wurden am 19.09.2026 einmalig
live abgerufen - nur JSON-Endpunkte (System/Body/Meeting/Consultation/Paper); PDF-Downloads dieser
Quelle wurden bewusst nicht abgerufen, siehe radar/sources/oparl.py (robots.txt)."""

import json
from dataclasses import replace
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
from radar.sources.base import Sitzung, SourceLayoutError
from radar.sources.oparl import OParlAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "uplengen"
HOST = "https://uplengen.ratsinfomanagement.net"
API = f"{HOST}/webservice/oparl/v1.1"
SYSTEM_URL = f"{API}/system"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


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
        id="uplengen",
        name="Gemeinde Uplengen",
        bundesland="NI",
        system="oparl",
        base_url=SYSTEM_URL,
        status="verified",
        enabled=False,  # robots.txt sperrt PDF-Downloads dieser Quelle, siehe radar/sources/oparl.py
    )


def _mock_robots(requests_mock):
    # Echter Inhalt der Uplengen-robots.txt: sperrt nur PDF-Downloads (Wildcard-Muster, siehe
    # radar/http_client.py und tests/test_http_client.py), sonst alles erlaubt.
    requests_mock.get(f"{HOST}/robots.txt", text="User-agent: *\nDisallow: /*.pdf$\n")


@pytest.fixture
def adapter(source, settings, requests_mock):
    _mock_robots(requests_mock)
    http = HttpClient(source, settings)
    return OParlAdapter(source, http)


def _mock_system_and_body(requests_mock):
    requests_mock.get(SYSTEM_URL, text=_load("system.json"))
    requests_mock.get(f"{API}/body", text=_load("body.json"))


def _mock_meeting_pages(requests_mock):
    requests_mock.get(f"{API}/body/1/meeting", text=_load("meeting_page1.json"))
    requests_mock.get(f"{API}/body/1/meeting?page=15", text=_load("meeting_page2.json"))


def _mock_meeting_1853_with_papers(requests_mock):
    requests_mock.get(f"{API}/body/1/meeting/1853", text=_load("meeting_1853.json"))
    requests_mock.get(f"{API}/body/1/consultation/2473", text=_load("consultation_2473.json"))
    requests_mock.get(f"{API}/body/1/paper/1508", text=_load("paper_1508.json"))
    requests_mock.get(f"{API}/body/1/consultation/2493", text=_load("consultation_2493.json"))
    requests_mock.get(f"{API}/body/1/paper/1520", text=_load("paper_1520.json"))
    requests_mock.get(f"{API}/body/1/consultation/2469", text=_load("consultation_2469.json"))
    requests_mock.get(f"{API}/body/1/paper/1506", text=_load("paper_1506.json"))


# -- list_sessions ------------------------------------------------------------------------------


def test_list_sessions_paginates_and_filters_by_date_range(requests_mock, adapter):
    """Die Sitzungsliste ist über zwei Seiten verteilt (`links.next`), Sitzungen darauf sind nicht
    chronologisch sortiert (die letzte Seite enthält u. a. die am spätesten angelegten, aber
    zeitlich früher liegenden Termine) - der Adapter muss deshalb beide Seiten vollständig lesen
    und selbst nach Datum filtern, statt sich auf eine Sortierreihenfolge zu verlassen."""
    _mock_system_and_body(requests_mock)
    _mock_meeting_pages(requests_mock)

    page1 = json.loads(_load("meeting_page1.json"))["data"]
    page2 = json.loads(_load("meeting_page2.json"))["data"]
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    expected = sum(1 for m in page1 + page2 if start <= date.fromisoformat(m["start"][:10]) <= end)
    assert expected > 0

    sessions = adapter.list_sessions(start, end)

    assert len(sessions) == expected
    assert all(start <= s.datum <= end for s in sessions)
    assert not any(s.datum.year == 2025 for s in sessions)  # Seite 1 enthält auch 2025er Termine


def test_list_sessions_parses_gremium_datum_ort_uhrzeit(requests_mock, adapter):
    _mock_system_and_body(requests_mock)
    _mock_meeting_pages(requests_mock)

    sessions = adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))
    sitzung = next(s for s in sessions if s.datum == date(2026, 3, 18))

    assert sitzung.gremium == "Ausschuss für Umwelt und Verkehr"
    assert sitzung.uhrzeit == "19:30"
    assert sitzung.ort is not None and "Uplengen" in sitzung.ort
    assert sitzung.url == f"{API}/body/1/meeting/1943"
    assert sitzung.external_id == sitzung.url
    assert sitzung.abgesagt is False


def test_list_sessions_strips_sitzung_suffix_from_gremium_name(requests_mock, adapter):
    _mock_system_and_body(requests_mock)
    _mock_meeting_pages(requests_mock)

    sessions = adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))
    gemeinderat = next(s for s in sessions if s.datum == date(2026, 3, 19))
    assert gemeinderat.gremium == "Gemeinderat"  # "(20. Sitzung)" wurde entfernt


def test_list_sessions_marks_cancelled_meeting(requests_mock, adapter):
    """Kein echtes Beispiel bei Uplengen gefunden - `cancelled` laut OParl-1.1-Spezifikation
    konstruiert, um die Zuordnung auf `Sitzung.abgesagt` zu prüfen."""
    _mock_system_and_body(requests_mock)
    requests_mock.get(
        f"{API}/body/1/meeting",
        json={
            "data": [
                {
                    "id": f"{API}/body/1/meeting/9001",
                    "name": "Gemeinderat (99. Sitzung)",
                    "start": "2026-05-01T18:00:00+02:00",
                    "cancelled": True,
                }
            ],
            "links": {},
        },
    )
    sessions = adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))
    assert len(sessions) == 1
    assert sessions[0].abgesagt is True


def test_list_sessions_rejects_response_without_oparl_fingerprint(requests_mock, adapter):
    requests_mock.get(SYSTEM_URL, json={"not": "oparl"})
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))


def test_list_sessions_rejects_non_json_response(requests_mock, adapter):
    requests_mock.get(SYSTEM_URL, text="<html>Wartungsmodus</html>")
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))


def test_list_sessions_rejects_multiple_bodies_without_configured_index(requests_mock, adapter):
    """Ein 'Amt' kann mehrere Kommunen (Bodies) in einem System bündeln (CLAUDE.md: in SH/MV
    betreiben Ämter teils ein gemeinsames Ratsinformationssystem) - dann muss `body_index`
    explizit gesetzt werden, statt still die erste Body zu verwenden."""
    requests_mock.get(SYSTEM_URL, text=_load("system.json"))
    requests_mock.get(
        f"{API}/body",
        json={
            "data": [
                {"id": f"{API}/body/1", "name": "Gemeinde A", "meeting": f"{API}/body/1/meeting"},
                {"id": f"{API}/body/2", "name": "Gemeinde B", "meeting": f"{API}/body/2/meeting"},
            ],
            "pagination": {"totalPages": 1},
        },
    )
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))


def test_list_sessions_uses_configured_body_index(requests_mock, source, settings):
    src = replace(source, options={"body_index": 1})
    _mock_robots(requests_mock)
    requests_mock.get(SYSTEM_URL, text=_load("system.json"))
    requests_mock.get(
        f"{API}/body",
        json={
            "data": [
                {"id": f"{API}/body/1", "name": "Gemeinde A", "meeting": f"{API}/body/1/meeting_a"},
                {"id": f"{API}/body/2", "name": "Gemeinde B", "meeting": f"{API}/body/2/meeting_b"},
            ],
            "pagination": {"totalPages": 1},
        },
    )
    requests_mock.get(f"{API}/body/2/meeting_b", json={"data": [], "links": {}})
    adapter = OParlAdapter(src, HttpClient(src, settings))

    sessions = adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))

    assert sessions == []  # kein Fehler -> es wurde tatsächlich body_index=1 (meeting_b) angefragt


# -- list_documents -------------------------------------------------------------------------------


def test_list_documents_returns_empty_when_no_url(adapter):
    sitzung = Sitzung(source_id="uplengen", external_id="x", gremium="G", datum=date(2026, 1, 1), url=None)
    assert adapter.list_documents(sitzung) == []


def test_list_documents_groups_by_paper_across_agenda_and_meeting(requests_mock, adapter):
    """Reale Tagesordnung mit drei Bebauungsplan-Vorlagen (je `consultation` -> `paper`) und einem
    Tagesordnungspunkt ganz ohne Vorlage (Genehmigung einer Niederschrift, nur `resolutionFile`).
    Prüft: Haupt-/Anlagen-Dokumente eines Papers werden über `paper.id` gruppiert (die Vorlagen-Nr.
    stammt aus `paper.reference`), sitzungsweite Dokumente bleiben ohne Vorlagen-Bezug, und ein
    Tagesordnungspunkt ohne Paper bekommt eine eigene, auf sich selbst beschränkte Gruppierung."""
    _mock_meeting_1853_with_papers(requests_mock)
    meeting_url = f"{API}/body/1/meeting/1853"
    sitzung = Sitzung(
        source_id="uplengen",
        external_id=meeting_url,
        gremium="Bau- und Entwicklungsausschuss",
        datum=date(2025, 11, 18),
        url=meeting_url,
    )

    documents = adapter.list_documents(sitzung)

    paper_id = f"{API}/body/1/paper/1508"
    vorlage_docs = [d for d in documents if d.vorlage_external_id == paper_id]
    assert len(vorlage_docs) == 4  # mainFile + 2 Anlagen (Paper) + Beschlusstext (Tagesordnungspunkt)
    assert all(d.vorlage_nr == "126/2025" for d in vorlage_docs)
    assert {d.typ for d in vorlage_docs} == {"vorlage", "anlage", "beschluss"}

    standalone_id = f"{API}/body/1/agendaitem/7595"
    andere_vorlagen = {d.vorlage_external_id for d in documents} - {paper_id, standalone_id, None}
    assert andere_vorlagen == {f"{API}/body/1/paper/1520", f"{API}/body/1/paper/1506"}

    standalone = [d for d in documents if d.vorlage_external_id == standalone_id]
    assert len(standalone) == 1
    assert standalone[0].typ == "beschluss"  # "Genehmigung der Niederschrift", kein Paper

    meeting_docs = [d for d in documents if d.vorlage_external_id is None]
    assert {d.typ for d in meeting_docs} == {"einladung", "niederschrift"}

    for d in documents:
        assert d.source_id == "uplengen"
        assert d.sitzung_external_id == meeting_url
        assert d.url.startswith(f"{API}/body/1/files/download/")


def test_list_documents_excludes_nonpublic_agenda_items(requests_mock, adapter):
    """Kein echtes Beispiel bei Uplengen gefunden (nichtöffentliche Punkte tauchen dort schlicht
    nicht in der OParl-Antwort auf) - `public: false` probehalber injiziert, für den Fall, dass ein
    anderer OParl-Anbieter das Feld tatsächlich befüllt (CLAUDE.md: nur öffentliche Unterlagen)."""
    meeting = json.loads(_load("meeting_1853.json"))
    meeting["agendaItem"].append(
        {
            "id": f"{API}/body/1/agendaitem/9999999",
            "number": "8.",
            "name": "Nichtöffentlicher Punkt",
            "public": False,
            "resolutionFile": {
                "id": f"{API}/body/1/file/9-1",
                "name": "Geheimes Dokument",
                "downloadUrl": f"{API}/body/1/files/download/geheim.pdf",
            },
        }
    )
    requests_mock.get(f"{API}/body/1/meeting/1853", json=meeting)
    requests_mock.get(f"{API}/body/1/consultation/2473", text=_load("consultation_2473.json"))
    requests_mock.get(f"{API}/body/1/paper/1508", text=_load("paper_1508.json"))
    requests_mock.get(f"{API}/body/1/consultation/2493", text=_load("consultation_2493.json"))
    requests_mock.get(f"{API}/body/1/paper/1520", text=_load("paper_1520.json"))
    requests_mock.get(f"{API}/body/1/consultation/2469", text=_load("consultation_2469.json"))
    requests_mock.get(f"{API}/body/1/paper/1506", text=_load("paper_1506.json"))

    sitzung = Sitzung(
        source_id="uplengen", external_id="x", gremium="G", datum=date(2025, 11, 18), url=f"{API}/body/1/meeting/1853"
    )
    documents = adapter.list_documents(sitzung)
    assert not any(d.external_id == f"{API}/body/1/file/9-1" for d in documents)


def test_list_documents_skips_files_without_downloadable_url(requests_mock, adapter):
    """Nicht jede OParl-API liefert Dateien mit (CLAUDE.md) - ein File-Objekt ganz ohne
    `downloadUrl`/`accessUrl` darf kein Dokument erzeugen."""
    requests_mock.get(
        f"{API}/body/1/meeting/1",
        json={
            "id": f"{API}/body/1/meeting/1",
            "invitation": {"id": f"{API}/body/1/file/1", "name": "Ohne URL"},
            "agendaItem": [],
        },
    )
    sitzung = Sitzung(
        source_id="uplengen", external_id="x", gremium="G", datum=date(2026, 1, 1), url=f"{API}/body/1/meeting/1"
    )
    assert adapter.list_documents(sitzung) == []
