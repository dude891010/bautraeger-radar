"""Tests für den bv-hh.de-Adapter (radar/sources/bv_hh.py) gegen echte, gespeicherte Antworten
der Bergedorf-Instanz (tests/fixtures/bv_hh/). Kein Netzzugriff: `requests_mock` ersetzt den
Transport. Die Fixtures wurden am 19.09.2026 einmalig live abgerufen."""

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
from radar.sources.bv_hh import BvHhAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "bv_hh"
BASE_URL = "https://bv-hh.de/bergedorf/"
HOST = "https://bv-hh.de"

# Minimale, aber layoutgetreue Seite ohne echten Inhalt - für Fehlerfall-Tests (bewahrt den
# navbar-brand-Fingerprint, damit gezielt nur das jeweils geprüfte Layoutmerkmal fehlt).
_SHELL = '<html><body><a class="navbar-brand" href="/bergedorf">BV-HH</a>{body}</body></html>'


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
        id="bv_hh_bergedorf",
        name="bv-hh.de (Bergedorf)",
        bundesland="HH",
        system="bv_hh",
        base_url=BASE_URL,
        status="verified",
        enabled=False,
    )


def _mock_robots(requests_mock):
    requests_mock.get(f"{HOST}/robots.txt", text="User-agent: *\nAllow: /\n")


@pytest.fixture
def adapter(source, settings, requests_mock):
    _mock_robots(requests_mock)
    http = HttpClient(source, settings)
    return BvHhAdapter(source, http)


# -- list_sessions ------------------------------------------------------------------------------


def test_list_sessions_parses_rows_and_stops_mid_page(requests_mock, adapter):
    """meetings_page1.html enthält 25 Sitzungen vom 13.10. bis 17.12.2026 - mit einem Startdatum
    mitten in dieser Spanne muss die absteigend sortierte Liste vorzeitig abbrechen, ohne eine
    zweite Seite abzurufen (kein Mock für page=2 registriert - ein Zugriffsversuch ließe den Test
    mit einem NoMockAddress-Fehler scheitern)."""
    requests_mock.get(f"{BASE_URL}meetings?page=1", text=_load("meetings_page1.html"))

    sessions = adapter.list_sessions(date(2026, 11, 1), date(2026, 12, 31))

    assert all(date(2026, 11, 1) <= s.datum <= date(2026, 12, 31) for s in sessions)
    assert not any(s.datum < date(2026, 11, 1) for s in sessions)

    bauausschuss = next(s for s in sessions if s.external_id == "7437")
    assert bauausschuss.gremium == "Fachausschuss für Bauangelegenheiten"
    assert bauausschuss.datum == date(2026, 12, 16)
    assert bauausschuss.uhrzeit == "18:00"
    assert bauausschuss.url == "/bergedorf/meetings/16-12-2026-sitzung-des-fachausschusses-fuer-bauangelegenheiten-7437"
    assert bauausschuss.abgesagt is False


def test_list_sessions_paginates_when_older_meetings_are_needed(requests_mock, adapter):
    """Mit einem Startdatum vor der ältesten Sitzung auf Seite 1 muss eine zweite Seite abgerufen
    werden. Seite 2 ist hier eine minimale, aber layoutgetreue synthetische Seite - der Zugriff
    selbst (nicht ihr Inhalt) ist der Punkt dieses Tests."""
    requests_mock.get(f"{BASE_URL}meetings?page=1", text=_load("meetings_page1.html"))
    def _row(datum: str, external_id: str) -> str:
        href = f"/bergedorf/meetings/{datum}-sitzung-des-hauptausschusses-{external_id}"
        return (
            f'<div class="row pt-3 border-top">'
            f'<div class="col-3 col-lg-2"><a href="{href}"><strong>{datum.replace("-", ".")}</strong></a>'
            f"<br />18:00 </div>"
            f'<div class="col-7 col-lg-6"><a href="{href}">Sitzung des Hauptausschusses</a></div>'
            f'<div class="col-9 col-lg offset-3 offset-lg-0 pt-2 pt-lg-0">'
            f'<a class="text-secondary" href="/bergedorf/committees/122">Hauptausschuss</a></div></div>'
        )

    # Zweite Zeile liegt vor `start` (2026-09-01) -> Abbruchbedingung greift, keine Seite 3 nötig.
    page2 = _row("01-09-2026", "7100") + _row("01-08-2026", "7050")
    requests_mock.get(f"{BASE_URL}meetings?page=2", text=_SHELL.format(body=page2))

    sessions = adapter.list_sessions(date(2026, 9, 1), date(2026, 12, 31))

    assert any(s.external_id == "7100" for s in sessions)  # kam nur von Seite 2


def test_list_sessions_rejects_page_without_bvhh_fingerprint(requests_mock, adapter):
    requests_mock.get(f"{BASE_URL}meetings?page=1", text="<html><body>keine bv-hh-Seite</body></html>")
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))


def test_list_sessions_rejects_empty_first_page(requests_mock, adapter):
    requests_mock.get(f"{BASE_URL}meetings?page=1", text=_SHELL.format(body=""))
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))


# -- list_documents -------------------------------------------------------------------------------


def test_list_documents_returns_empty_when_no_url(adapter):
    sitzung = Sitzung(source_id="bv_hh_bergedorf", external_id="x", gremium="G", datum=date(2026, 1, 1), url=None)
    assert adapter.list_documents(sitzung) == []


def _mock_stadtentwicklung_meeting(requests_mock):
    meeting_url = f"{HOST}/bergedorf/meetings/03-06-2026-sitzung-des-stadtentwicklungsausschusses-6890"
    requests_mock.get(meeting_url, text=_load("meeting_6890_stadtentwicklung.html"))
    dokument_slugs = [
        (
            "entwicklungsvorhaben-weidensteg-quartier-aktueller-sachstand-und-"
            "vorstellung-der-ueberarbeiteten-entwurfsplanung-fuer-baufeld-7-227087",
            "document_weidensteg.html",
        ),
        (
            "grundschule-im-garten-quartier-in-oberbillwerder-hier-vorstellung-der-"
            "auslobungsunterlage-textteil-227089",
            "document_grundschule.html",
        ),
        (
            "bebauungsplanverfahren-kirchwerder-34-suedlich-karkenland-hier-zustimmung-"
            "zur-oeffentlichkeitsbeteiligung-internetveroeffentlichung-227091",
            "document_kirchwerder.html",
        ),
        (
            "machbarkeitsstudie-zum-neubau-eines-gemeinschaftshauses-in-bergedorf-west-227092",
            "document_gemeinschaftshaus.html",
        ),
    ]
    for slug, fixture in dokument_slugs:
        requests_mock.get(f"{HOST}/bergedorf/documents/{slug}", text=_load(fixture))
    return meeting_url


def test_list_documents_collects_referenced_drucksachen_with_text(requests_mock, adapter):
    """Reale Tagesordnung mit 9 Punkten, davon 4 mit verlinkter Drucksache (die anderen sind rein
    prozedural, z. B. Begrüßung) - jede Drucksache bekommt ihren Text aus dem JSON-LD `articleBody`
    statt einer PDF-URL (bv-hh liefert keine Anlagen, siehe radar/sources/bv_hh.py)."""
    meeting_url = _mock_stadtentwicklung_meeting(requests_mock)
    sitzung = Sitzung(
        source_id="bv_hh_bergedorf",
        external_id="6890",
        gremium="Stadtentwicklungsausschuss",
        datum=date(2026, 6, 3),
        url=meeting_url,
    )

    documents = adapter.list_documents(sitzung)

    assert len(documents) == 4
    by_nr = {d.vorlage_nr: d for d in documents}
    assert set(by_nr) == {"22-0388.01", "22-0639.01", "22-0848", "22-0795"}

    kirchwerder = by_nr["22-0848"]
    assert kirchwerder.typ == "drucksache"
    assert kirchwerder.vorlage_external_id == kirchwerder.external_id == "227091"
    assert kirchwerder.sitzung_external_id == "6890"
    assert "Bebauungsplan Kirchwerder 34" in kirchwerder.text
    assert "Wohngebiet" in kirchwerder.text
    assert "&nbsp;" not in kirchwerder.text  # HTML-Entities aufgelöst

    for d in documents:
        assert d.source_id == "bv_hh_bergedorf"
        assert d.text is not None
        assert d.url.startswith("/bergedorf/documents/")


def test_list_documents_returns_nothing_for_purely_procedural_meeting(requests_mock, adapter):
    meeting_url = f"{HOST}/bergedorf/meetings/23-09-2026-sitzung-des-fachausschusses-fuer-bauangelegenheiten-6874"
    requests_mock.get(meeting_url, text=_load("meeting_6874_procedural.html"))
    sitzung = Sitzung(
        source_id="bv_hh_bergedorf", external_id="6874", gremium="G", datum=date(2026, 9, 23), url=meeting_url
    )
    assert adapter.list_documents(sitzung) == []


def test_list_documents_excludes_nonpublic_topics(requests_mock, adapter):
    """Kein echtes Beispiel bei Bergedorf gefunden (alle geprüften TOPs waren 'Ö') - ein
    nichtöffentlicher Punkt probehalber injiziert, analog zum SessionNet-Test (CLAUDE.md: nur
    öffentliche Unterlagen)."""
    html = _load("meeting_6890_stadtentwicklung.html")
    injected = html.replace(
        "</body>",
        '<div class="row pt-3 border-top">'
        '<div class="col-2 col-lg-1 py-1"><strong class="text-nowrap">N 10</strong></div>'
        '<div class="col col-lg-9 py-1"><a href="/bergedorf/documents/geheime-drucksache-999999">Geheim</a></div>'
        '<div class="col-10 col-sm-2 offset-2 offset-sm-0 text-end">'
        '<a href="/bergedorf/documents/geheime-drucksache-999999"><strong>22-9999</strong></a></div>'
        "</div></body>",
    )
    meeting_url = _mock_stadtentwicklung_meeting(requests_mock)
    requests_mock.get(meeting_url, text=injected)

    sitzung = Sitzung(
        source_id="bv_hh_bergedorf", external_id="6890", gremium="G", datum=date(2026, 6, 3), url=meeting_url
    )
    documents = adapter.list_documents(sitzung)
    assert not any(d.external_id == "999999" for d in documents)


def test_list_documents_skips_topic_when_document_has_no_article_body(requests_mock, adapter):
    meeting_url = f"{HOST}/bergedorf/meetings/test-meeting-1"
    requests_mock.get(
        meeting_url,
        text=_SHELL.format(
            body=(
                '<div class="row pt-3 border-top">'
                '<div class="col-2 col-lg-1 py-1"><strong class="text-nowrap">Ö 1</strong></div>'
                '<div class="col col-lg-9 py-1"><a href="/bergedorf/documents/ohne-inhalt-123">Ohne Inhalt</a></div>'
                '<div class="col-10 col-sm-2 offset-2 offset-sm-0 text-end">'
                '<a href="/bergedorf/documents/ohne-inhalt-123"><strong>22-0001</strong></a></div></div>'
            )
        ),
    )
    requests_mock.get(
        f"{HOST}/bergedorf/documents/ohne-inhalt-123", text=_SHELL.format(body="<p>Kein JSON-LD hier.</p>")
    )

    sitzung = Sitzung(
        source_id="bv_hh_bergedorf", external_id="test-meeting-1", gremium="G", datum=date(2026, 1, 1), url=meeting_url
    )
    assert adapter.list_documents(sitzung) == []
