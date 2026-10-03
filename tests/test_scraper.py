"""Tests für die Lauf-Orchestrierung (radar/scraper.py). Nutzt Fake-Adapter/-HTTP-Clients statt
echtem Netzzugriff, damit die Isolations- und Zähllogik unabhängig von SessionNet getestet wird
(siehe CLAUDE.md: "Netzwerk ... hinter dünnen Schnittstellen, damit Tests sie ersetzen können")."""

from datetime import date

import pytest

from radar import database
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
from radar.parser import LLMBudget, ResultCache
from radar.scraper import (
    LaufLaeuftBereits,
    SourceRunResult,
    _collect,
    _collect_and_persist,
    _shift_months,
    lauf_sperre,
    run,
    run_full,
    run_source,
    run_source_full,
    scan_range,
)
from radar.sources.base import Dokument, Sitzung
from tests.conftest import FakeAnthropicClient, make_pdf


class FakeAdapter:
    def __init__(self, sessions, documents_by_session=None, fail_on_documents_for=None):
        self._sessions = sessions
        self._documents_by_session = documents_by_session or {}
        self._fail_on_documents_for = fail_on_documents_for or set()

    def list_sessions(self, start, end):
        return self._sessions

    def list_documents(self, sitzung):
        if sitzung.external_id in self._fail_on_documents_for:
            raise RuntimeError(f"Unterlagen für {sitzung.external_id} nicht ladbar")
        return self._documents_by_session.get(sitzung.external_id, [])


class FakeHttp:
    def __init__(self, fail_ids=None):
        self.downloaded: list[str] = []
        self._fail_ids = fail_ids or set()

    def download_pdf(self, url, external_id):
        if external_id in self._fail_ids:
            raise RuntimeError(f"Download {external_id} fehlgeschlagen")
        self.downloaded.append(external_id)


def _sitzung(external_id, gremium, abgesagt=False):
    return Sitzung(
        source_id="test", external_id=external_id, gremium=gremium, datum=date(2026, 9, 1), abgesagt=abgesagt
    )


def _dokument(external_id, vorlage_id=None):
    return Dokument(
        source_id="test",
        external_id=external_id,
        titel=f"Dokument {external_id}",
        url=f"getfile.php?id={external_id}",
        sitzung_external_id="s1",
        vorlage_external_id=vorlage_id,
    )


def _text_dokument(external_id, text, vorlage_id=None):
    """Dokument einer Quelle ohne eigenes PDF (Dokument.text gesetzt), siehe radar/sources/bv_hh.py."""
    return Dokument(
        source_id="test",
        external_id=external_id,
        titel=f"Dokument {external_id}",
        url=f"https://example.org/{external_id}",  # wird nicht abgerufen
        sitzung_external_id="s1",
        vorlage_external_id=vorlage_id,
        text=text,
    )


PATTERNS = ["stadtentwicklung"]


# -- _collect -------------------------------------------------------------------------------------


def test_collect_only_counts_matching_committees():
    sessions = [
        _sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr"),
        _sitzung("2", "Kulturausschuss"),  # passt nicht zu PATTERNS
    ]
    adapter = FakeAdapter(sessions, documents_by_session={"1": [_dokument("d1")]})
    http = FakeHttp()
    result = SourceRunResult(source_id="test")

    _collect(adapter, http, PATTERNS, date(2026, 9, 1), date(2026, 9, 30), result)

    assert result.sitzungen == 2
    assert result.relevante_sitzungen == 1
    assert result.dokumente == 1
    assert result.heruntergeladen == 1
    assert http.downloaded == ["d1"]
    assert result.fehler == []


def test_collect_skips_cancelled_sessions():
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr", abgesagt=True)]
    adapter = FakeAdapter(sessions, documents_by_session={"1": [_dokument("d1")]})
    result = SourceRunResult(source_id="test")

    _collect(adapter, FakeHttp(), PATTERNS, date(2026, 9, 1), date(2026, 9, 30), result)

    assert result.sitzungen == 1
    assert result.relevante_sitzungen == 0
    assert result.dokumente == 0


def test_collect_deduplicates_vorlagen_across_sessions():
    sessions = [
        _sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr"),
        _sitzung("2", "Ausschuss für Stadtentwicklung und Verkehr"),
    ]
    adapter = FakeAdapter(
        sessions,
        documents_by_session={
            "1": [_dokument("d1", vorlage_id="V1"), _dokument("d2", vorlage_id="V1")],
            "2": [_dokument("d3", vorlage_id="V1"), _dokument("d4", vorlage_id="V2")],
        },
    )
    result = SourceRunResult(source_id="test")

    _collect(adapter, FakeHttp(), PATTERNS, date(2026, 9, 1), date(2026, 9, 30), result)

    assert result.dokumente == 4
    assert result.vorlagen == 2  # V1 und V2, unabhängig davon wie oft sie vorkommen


def test_collect_isolates_document_listing_failure():
    sessions = [
        _sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr"),
        _sitzung("2", "Ausschuss für Stadtentwicklung und Verkehr"),
    ]
    adapter = FakeAdapter(
        sessions,
        documents_by_session={"2": [_dokument("d1")]},
        fail_on_documents_for={"1"},
    )
    http = FakeHttp()
    result = SourceRunResult(source_id="test")

    _collect(adapter, http, PATTERNS, date(2026, 9, 1), date(2026, 9, 30), result)

    assert result.relevante_sitzungen == 2  # beide zählen als relevant, auch die kaputte
    assert result.dokumente == 1  # nur die zweite Sitzung lieferte Dokumente
    assert http.downloaded == ["d1"]
    assert len(result.fehler) == 1
    assert "1" in result.fehler[0]


def test_collect_isolates_download_failure():
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    adapter = FakeAdapter(sessions, documents_by_session={"1": [_dokument("d1"), _dokument("d2")]})
    http = FakeHttp(fail_ids={"d1"})
    result = SourceRunResult(source_id="test")

    _collect(adapter, http, PATTERNS, date(2026, 9, 1), date(2026, 9, 30), result)

    assert result.dokumente == 2
    assert result.heruntergeladen == 1
    assert http.downloaded == ["d2"]
    assert len(result.fehler) == 1
    assert "d1" in result.fehler[0]


def test_collect_counts_text_documents_without_downloading():
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    adapter = FakeAdapter(sessions, documents_by_session={"1": [_text_dokument("d1", "irgendein Text")]})
    http = FakeHttp()
    result = SourceRunResult(source_id="test")

    _collect(adapter, http, PATTERNS, date(2026, 9, 1), date(2026, 9, 30), result)

    assert result.dokumente == 1
    assert result.text_dokumente == 1
    assert result.heruntergeladen == 0
    assert http.downloaded == []  # kein Downloadversuch für Dokument.text


# -- run_source -------------------------------------------------------------------------------------


def test_run_source_isolates_adapter_construction_failure(monkeypatch, settings_stub):
    def boom(source, http):
        raise RuntimeError("Systemtyp unbekannt")

    monkeypatch.setattr("radar.scraper.get_adapter", boom)
    monkeypatch.setattr("radar.scraper.HttpClient", lambda source, settings: FakeHttp())

    source = Source(id="broken", name="Kaputt", bundesland="SH", system="sessionnet", base_url="https://example.org/")
    result = run_source(source, settings_stub, date(2026, 9, 1), date(2026, 9, 30))

    assert result.ok is False
    assert "Systemtyp unbekannt" in result.fehler[0]


# -- run --------------------------------------------------------------------------------------------


def test_run_continues_after_one_source_fails(monkeypatch, settings_stub):
    good = Source(
        id="good", name="Gut", bundesland="SH", system="sessionnet", base_url="https://good.example.org/",
        status="verified", enabled=True, tier="A",
    )
    bad = Source(
        id="bad", name="Schlecht", bundesland="SH", system="sessionnet", base_url="https://bad.example.org/",
        status="verified", enabled=True, tier="A",
    )

    adapters = {
        "good": FakeAdapter([_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")], {"1": [_dokument("d1")]}),
    }

    def fake_get_adapter(source, http):
        if source.id not in adapters:
            raise RuntimeError(f"Quelle '{source.id}' kaputt")
        return adapters[source.id]

    monkeypatch.setattr("radar.scraper.load_settings", lambda: settings_stub)
    monkeypatch.setattr("radar.scraper.load_sources", lambda settings: [good, bad])
    monkeypatch.setattr("radar.scraper.HttpClient", lambda source, settings: FakeHttp())
    monkeypatch.setattr("radar.scraper.get_adapter", fake_get_adapter)

    results = run(settings_stub)

    by_id = {r.source_id: r for r in results}
    assert by_id["good"].ok
    assert by_id["good"].dokumente == 1
    assert not by_id["bad"].ok
    assert "kaputt" in by_id["bad"].fehler[0]


# -- scan_range / _shift_months ----------------------------------------------------------------------


def test_shift_months_caps_day_at_month_end():
    assert _shift_months(date(2026, 3, 31), -1) == date(2026, 2, 28)  # 2026 kein Schaltjahr
    assert _shift_months(date(2026, 1, 31), -1) == date(2025, 12, 31)
    assert _shift_months(date(2026, 1, 15), 2) == date(2026, 3, 15)


def test_scan_range_uses_settings(settings_stub):
    settings_stub.scan.months_back = 3
    settings_stub.scan.months_ahead = 2
    start, end = scan_range(settings_stub, today=date(2026, 9, 19))
    assert start == date(2026, 6, 19)
    assert end == date(2026, 11, 19)


@pytest.fixture
def settings_stub(monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(request_delay_seconds=0.0),
        filters=FiltersConfig(default_committee_patterns=PATTERNS),
        llm=LLMConfig(),
        database=DatabaseConfig(),
        dashboard=DashboardConfig(),
    )


# ===================================================================================================
# Verdrahtung: _collect_and_persist / run_source_full / run_full (Scraper -> Parser -> Datenbank)
# ===================================================================================================


class FakeHttpFiles:
    """Wie FakeHttp, liefert aber echte (Mini-)PDF-Dateien für `download_pdf`, damit
    `radar.parser.analyze_documents` (pdfplumber) etwas zu lesen hat."""

    def __init__(self, tmp_path, content_by_id=None, fail_ids=None):
        self._dir = tmp_path
        self._content_by_id = content_by_id or {}
        self._fail_ids = fail_ids or set()
        self.downloaded: list[str] = []

    def download_pdf(self, url, external_id):
        if external_id in self._fail_ids:
            raise RuntimeError(f"Download {external_id} fehlgeschlagen")
        self.downloaded.append(external_id)
        text = self._content_by_id.get(external_id, "Belanglose Seite ohne Bezug.")
        path = self._dir / f"{external_id}.pdf"
        path.write_bytes(make_pdf(text))
        return path


@pytest.fixture
def settings_full(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(request_delay_seconds=0.0),
        filters=FiltersConfig(default_committee_patterns=PATTERNS, prefilter_keywords=["Wohneinheit"]),
        llm=LLMConfig(
            triage_model="fake-triage",
            extraction_model="fake-extraction",
            max_calls_per_run=10,
            max_calls_per_source=10,
            cache_dir=str(tmp_path / "llm_cache"),
        ),
        database=DatabaseConfig(path=str(tmp_path / "radar.db")),
        dashboard=DashboardConfig(),
    )


@pytest.fixture
def conn(settings_full):
    connection = database.connect(settings_full)
    yield connection
    connection.close()


@pytest.fixture
def source_full():
    return Source(
        id="test",
        name="Stadt Test",
        bundesland="SH",
        system="sessionnet",
        base_url="https://example.org/",
        status="verified",
        enabled=True,
        tier="A",
    )


RELEVANT_TEXT = "Beschlussvorlage: 18 Wohneinheiten als Mietwohnungen. " * 5

_TRIAGE_JA = {"entscheidung": "JA"}
_EXTRAKTION_18_WE = {"we_gesamt": 18, "wohnform": "MIETE", "konfidenz": 0.9}


def test_collect_and_persist_relevant_vorlage_creates_project(tmp_path, conn, source_full, settings_full):
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    documents = [_dokument("d1", vorlage_id="v1"), _dokument("d2", vorlage_id="v1")]
    adapter = FakeAdapter(sessions, documents_by_session={"1": documents})
    http = FakeHttpFiles(tmp_path, content_by_id={"d1": RELEVANT_TEXT, "d2": RELEVANT_TEXT})
    client = FakeAnthropicClient([_TRIAGE_JA, _EXTRAKTION_18_WE])
    budget = LLMBudget(10, 10)
    cache = ResultCache(tmp_path / "cache")
    result = SourceRunResult(source_id="test")
    database.ensure_quelle(conn, source_full)

    _collect_and_persist(
        adapter, http, conn, client, budget, cache, source_full, settings_full,
        date(2026, 9, 1), date(2026, 9, 30), result,
    )

    assert result.vorlagen == 1
    assert result.dokumente == 2
    assert result.heruntergeladen == 2
    assert result.treffer == 1
    assert result.bereits_analysiert == 0
    assert result.fehler == []

    assert len(result.neue_treffer) == 1
    treffer = result.neue_treffer[0]
    assert treffer.kommune == "Stadt Test"  # source_full.name
    assert treffer.we_gesamt == 18
    assert treffer.url == "https://example.org/"  # keine Sitzungs-URL im Fixture -> source.base_url als Fallback

    vorlage_row = conn.execute("SELECT * FROM vorlagen").fetchone()
    assert vorlage_row["analyse_status"] == "RELEVANT"
    dokument_rows = conn.execute("SELECT * FROM dokumente").fetchall()
    assert len(dokument_rows) == 2
    assert all(row["sha256"] for row in dokument_rows)
    projekt_row = conn.execute("SELECT * FROM projekte").fetchone()
    assert projekt_row["we_gesamt"] == 18
    link = conn.execute("SELECT * FROM projekt_vorlage").fetchone()
    assert link["vorlage_id"] == vorlage_row["id"]
    assert link["projekt_id"] == projekt_row["id"]


def test_collect_and_persist_analyzes_text_documents_without_download(tmp_path, conn, source_full, settings_full):
    """Dokumente ohne eigenes PDF (Dokument.text, siehe radar/sources/bv_hh.py) werden direkt
    analysiert - kein Download, kein `sha256`/`dateipfad` in der Datenbank."""
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    documents = [_text_dokument("d1", RELEVANT_TEXT, vorlage_id="v1")]
    adapter = FakeAdapter(sessions, documents_by_session={"1": documents})
    http = FakeHttp()  # download_pdf darf gar nicht aufgerufen werden
    client = FakeAnthropicClient([_TRIAGE_JA, _EXTRAKTION_18_WE])
    result = SourceRunResult(source_id="test")
    database.ensure_quelle(conn, source_full)

    _collect_and_persist(
        adapter, http, conn, client, LLMBudget(10, 10), ResultCache(tmp_path / "cache"), source_full, settings_full,
        date(2026, 9, 1), date(2026, 9, 30), result,
    )

    assert result.text_dokumente == 1
    assert result.heruntergeladen == 0
    assert result.treffer == 1
    assert http.downloaded == []
    dokument_row = conn.execute("SELECT * FROM dokumente").fetchone()
    assert dokument_row["sha256"] is None
    projekt_row = conn.execute("SELECT * FROM projekte").fetchone()
    assert projekt_row["we_gesamt"] == 18


def test_collect_and_persist_session_level_documents_not_analyzed(tmp_path, conn, source_full, settings_full):
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    documents = [_dokument("d1", vorlage_id=None)]
    adapter = FakeAdapter(sessions, documents_by_session={"1": documents})
    http = FakeHttpFiles(tmp_path)
    client = FakeAnthropicClient([])  # darf gar nicht aufgerufen werden
    result = SourceRunResult(source_id="test")
    database.ensure_quelle(conn, source_full)

    _collect_and_persist(
        adapter, http, conn, client, LLMBudget(10, 10), ResultCache(tmp_path / "cache"), source_full, settings_full,
        date(2026, 9, 1), date(2026, 9, 30), result,
    )

    assert result.vorlagen == 0
    assert client.calls == []
    dokument_row = conn.execute("SELECT * FROM dokumente").fetchone()
    assert dokument_row["vorlage_id"] is None
    assert dokument_row["sha256"]  # trotzdem heruntergeladen und gespeichert


def test_collect_and_persist_skips_unchanged_settled_vorlage_on_rerun(tmp_path, conn, source_full, settings_full):
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    documents = [_dokument("d1", vorlage_id="v1")]
    adapter = FakeAdapter(sessions, documents_by_session={"1": documents})
    content = {"d1": RELEVANT_TEXT}
    client = FakeAnthropicClient([_TRIAGE_JA, _EXTRAKTION_18_WE])
    budget = LLMBudget(10, 10)
    cache = ResultCache(tmp_path / "cache")
    database.ensure_quelle(conn, source_full)

    result1 = SourceRunResult(source_id="test")
    _collect_and_persist(
        adapter, FakeHttpFiles(tmp_path, content), conn, client, budget, cache, source_full, settings_full,
        date(2026, 9, 1), date(2026, 9, 30), result1,
    )
    assert result1.treffer == 1
    assert len(client.calls) == 2

    result2 = SourceRunResult(source_id="test")
    _collect_and_persist(
        adapter, FakeHttpFiles(tmp_path, content), conn, client, budget, cache, source_full, settings_full,
        date(2026, 9, 1), date(2026, 9, 30), result2,
    )
    assert result2.bereits_analysiert == 1
    assert result2.treffer == 0  # kein neuer Treffer in diesem Lauf, war schon erledigt
    assert len(client.calls) == 2  # keine weiteren LLM-Aufrufe


def test_collect_and_persist_second_occurrence_of_same_project_is_not_a_new_treffer(
    tmp_path, conn, source_full, settings_full
):
    """Dasselbe Projekt (gleiche Adresse) taucht in einer zweiten Sitzung/Vorlage erneut auf
    (CLAUDE.md: "ein Projekt taucht in mehreren Gremien und Sitzungen auf") - `result.treffer`
    zählt beide Vorlagen, `result.neue_treffer` (für radar/notifier.py) nur die echte Neuanlage."""
    sessions = [
        _sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr"),
        _sitzung("2", "Ausschuss für Stadtentwicklung und Verkehr"),
    ]
    adapter = FakeAdapter(
        sessions,
        documents_by_session={
            "1": [_dokument("d1", vorlage_id="v1")],
            "2": [_dokument("d2", vorlage_id="v2")],
        },
    )
    dieselbe_adresse = {
        "we_gesamt": 18,
        "wohnform": "MIETE",
        "konfidenz": 0.9,
        "adresse": {"strasse": "Ochsenzollstraße", "hausnummer": "12", "ort": "Norderstedt"},
    }
    client = FakeAnthropicClient([_TRIAGE_JA, dieselbe_adresse, _TRIAGE_JA, dieselbe_adresse])
    budget = LLMBudget(10, 10)
    cache = ResultCache(tmp_path / "cache")
    database.ensure_quelle(conn, source_full)

    result = SourceRunResult(source_id="test")
    _collect_and_persist(
        adapter,
        FakeHttpFiles(tmp_path, {"d1": RELEVANT_TEXT, "d2": RELEVANT_TEXT}),
        conn, client, budget, cache, source_full, settings_full,
        date(2026, 9, 1), date(2026, 9, 30), result,
    )

    assert result.treffer == 2  # beide Vorlagen sind relevant
    assert len(result.neue_treffer) == 1  # aber nur eine echte Neuanlage
    assert conn.execute("SELECT COUNT(*) FROM projekte").fetchone()[0] == 1


def test_collect_and_persist_isolates_download_failure(tmp_path, conn, source_full, settings_full):
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    documents = [_dokument("d1", vorlage_id="v1"), _dokument("d2", vorlage_id="v1")]
    adapter = FakeAdapter(sessions, documents_by_session={"1": documents})
    http = FakeHttpFiles(tmp_path, content_by_id={"d2": RELEVANT_TEXT}, fail_ids={"d1"})
    client = FakeAnthropicClient([_TRIAGE_JA, _EXTRAKTION_18_WE])
    result = SourceRunResult(source_id="test")
    database.ensure_quelle(conn, source_full)

    _collect_and_persist(
        adapter, http, conn, client, LLMBudget(10, 10), ResultCache(tmp_path / "cache"), source_full, settings_full,
        date(2026, 9, 1), date(2026, 9, 30), result,
    )

    assert len(result.fehler) == 1
    assert "d1" in result.fehler[0]
    assert result.heruntergeladen == 1
    assert result.treffer == 1  # die verbliebene Anlage reicht für einen Treffer


def test_run_source_full_isolates_adapter_construction_failure(monkeypatch, tmp_path, conn, settings_full):
    def boom(source, http):
        raise RuntimeError("kaputt")

    monkeypatch.setattr("radar.scraper.get_adapter", boom)
    monkeypatch.setattr("radar.scraper.HttpClient", lambda source, settings: FakeHttpFiles(tmp_path))

    source = Source(id="broken", name="Kaputt", bundesland="SH", system="sessionnet", base_url="https://example.org/")
    result = run_source_full(
        source,
        settings_full,
        conn,
        FakeAnthropicClient([]),
        LLMBudget(10, 10),
        ResultCache(tmp_path / "cache"),
        date(2026, 9, 1),
        date(2026, 9, 30),
    )

    assert result.ok is False
    assert "kaputt" in result.fehler[0]
    # trotz Fehlschlag wurde die Quelle angelegt (Fremdschlüssel-Voraussetzung für einen künftigen Lauf)
    assert conn.execute("SELECT id FROM quellen WHERE id = 'broken'").fetchone() is not None


def test_run_full_continues_after_one_source_fails(monkeypatch, tmp_path, settings_full):
    good = Source(
        id="good", name="Gut", bundesland="SH", system="sessionnet", base_url="https://good.example.org/",
        status="verified", enabled=True, tier="A",
    )
    bad = Source(
        id="bad", name="Schlecht", bundesland="SH", system="sessionnet", base_url="https://bad.example.org/",
        status="verified", enabled=True, tier="A",
    )
    good_adapter = FakeAdapter(
        [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")],
        {"1": [_dokument("d1", vorlage_id="v1")]},
    )

    def fake_get_adapter(source, http):
        if source.id != "good":
            raise RuntimeError(f"Quelle '{source.id}' kaputt")
        return good_adapter

    monkeypatch.setattr("radar.scraper.load_sources", lambda settings: [good, bad])
    monkeypatch.setattr(
        "radar.scraper.HttpClient", lambda source, settings: FakeHttpFiles(tmp_path, {"d1": RELEVANT_TEXT})
    )
    monkeypatch.setattr("radar.scraper.get_adapter", fake_get_adapter)

    results = run_full(settings_full, client=FakeAnthropicClient([_TRIAGE_JA, _EXTRAKTION_18_WE]))

    by_id = {r.source_id: r for r in results}
    assert by_id["good"].ok
    assert by_id["good"].treffer == 1
    assert not by_id["bad"].ok
    assert "kaputt" in by_id["bad"].fehler[0]

    conn = database.connect(settings_full)
    try:
        assert {row["id"] for row in database.list_quellen(conn)} == {"good", "bad"}
        assert len(database.list_projekte(conn)) == 1
    finally:
        conn.close()


# -- Kostenschutz im Lauf: Notbremse sichtbar, kein Parallellauf ---------------------------------------


class _GuthabenLeerClient:
    """Simuliert ein leeres Anthropic-Guthaben (so am 19.09.2026 live beobachtet)."""

    def __init__(self):
        import anthropic
        import httpx2

        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        self._fehler = anthropic.BadRequestError(
            "Your credit balance is too low", response=httpx2.Response(400, request=request), body=None
        )
        self.calls = 0
        self.messages = self

    def parse(self, **kwargs):
        self.calls += 1
        raise self._fehler


def test_run_source_full_reports_emergency_brake_honestly(monkeypatch, tmp_path, conn, source_full, settings_full):
    sessions = [_sitzung("1", "Ausschuss für Stadtentwicklung und Verkehr")]
    documents = [_dokument(f"d{i}", vorlage_id=f"v{i}") for i in range(5)]  # 5 relevante Vorlagen
    adapter = FakeAdapter(sessions, documents_by_session={"1": documents})
    monkeypatch.setattr("radar.scraper.get_adapter", lambda source, http: adapter)
    monkeypatch.setattr(
        "radar.scraper.HttpClient",
        lambda source, settings: FakeHttpFiles(tmp_path, {f"d{i}": RELEVANT_TEXT for i in range(5)}),
    )
    client = _GuthabenLeerClient()

    result = run_source_full(
        source_full, settings_full, conn, client, LLMBudget(10, 10), ResultCache(tmp_path / "cache"),
        date(2026, 9, 1), date(2026, 9, 30),
    )

    assert client.calls == 1  # nur eine einzige Anfrage, nicht eine pro Vorlage
    assert result.llm_anfragen == 1
    assert result.ohne_llm == 5
    assert not result.ok  # landet im Quellen-Monitoring und im Exit-Code, nicht nur im Log
    assert any("Notbremse" in f and "credit balance" in f for f in result.fehler)
    statuses = {row["analyse_status"] for row in conn.execute("SELECT analyse_status FROM vorlagen")}
    assert statuses == {"BUDGET_ERSCHOEPFT"}  # nächster Lauf (mit Guthaben) versucht alle erneut


def test_run_full_refuses_to_start_while_another_run_is_active(monkeypatch, settings_full):
    monkeypatch.setattr("radar.scraper.load_sources", lambda settings: pytest.fail("darf gar nicht erst starten"))
    with lauf_sperre(settings_full):
        with pytest.raises(LaufLaeuftBereits):
            run_full(settings_full, client=FakeAnthropicClient([]))

