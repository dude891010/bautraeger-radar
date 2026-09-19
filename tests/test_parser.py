"""Tests für radar/parser.py: PDF-Text, Keyword-Vorfilter, Pydantic-Validierung, Budget/Cache und
die LLM-Aufrufe. Kein Netzzugriff – der Anthropic-Client wird durch ein Fake-Objekt ersetzt
(siehe CLAUDE.md: "Netzwerk und LLM hinter dünnen Schnittstellen, damit Tests sie ersetzen können")."""


import pytest
from pydantic import ValidationError

from radar.config import DashboardConfig, DatabaseConfig, FiltersConfig, LLMConfig, ScanConfig, ScraperConfig, Settings
from radar.parser import (
    Adresse,
    AnalyseStatus,
    LLMBudget,
    ProjektExtraktion,
    ResultCache,
    Textinhalt,
    TriageEntscheidung,
    Verfahrensstand,
    Wohnform,
    analyze_documents,
    build_excerpt,
    documents_cache_key,
    extract,
    extract_pdf_text,
    matched_keywords,
    triage,
)
from tests.conftest import FakeAnthropicClient
from tests.conftest import make_pdf as _make_pdf

KEYWORDS = ["Wohneinheit", "gefördert", "Mehrfamilienhaus"]


# -- PDF-Textextraktion --------------------------------------------------------------------------


def test_extract_pdf_text_reads_pages_with_markers(tmp_path):
    # Realistisch lange Seiten, damit die ocr_noetig-Heuristik (zu wenig Text je Seite) nicht
    # fälschlich anschlägt - echte Vorlagenseiten haben hunderte Zeichen, nicht nur ein paar Worte.
    seite1 = "Beschlussvorlage zur Errichtung eines Mehrfamilienhauses mit Mietwohnungen. " * 5
    seite2 = "Es entstehen insgesamt zwoelf Wohneinheiten, davon acht oeffentlich gefoerdert. " * 5
    path = tmp_path / "doc.pdf"
    path.write_bytes(_make_pdf(seite1, seite2))

    result = extract_pdf_text(path)

    assert result.ocr_noetig is False
    assert len(result.pages) == 2
    assert "Mehrfamilienhauses" in result.pages[0]
    assert "[Seite 1]" in result.text
    assert "[Seite 2]" in result.text


def test_extract_pdf_text_flags_ocr_noetig_for_empty_pages(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(_make_pdf(None, None))

    result = extract_pdf_text(path)

    assert result.ocr_noetig is True


# -- Stufe 1: Keyword-Vorfilter ------------------------------------------------------------------


def test_matched_keywords_is_case_insensitive():
    assert matched_keywords("Es entstehen 12 WOHNEINHEITEN.", ["Wohneinheit"]) == ["Wohneinheit"]


def test_matched_keywords_returns_empty_for_no_hit():
    assert matched_keywords("Es geht um Straßenbau.", KEYWORDS) == []


def test_build_excerpt_includes_only_hit_pages_and_neighbors():
    pages = ["Straßenbau.", "12 Wohneinheiten geplant.", "Haushaltsplan.", "Nichts Relevantes."]
    excerpt = build_excerpt(pages, ["Wohneinheit"], context_pages=1, max_chars=10_000)

    assert "[Seite 1]" in excerpt  # Nachbarseite des Treffers (Seite 2)
    assert "[Seite 2]" in excerpt
    assert "[Seite 3]" in excerpt  # ebenfalls Nachbarseite des Treffers (Distanz 1)
    assert "[Seite 4]" not in excerpt  # Distanz 2, kein Treffer -> nicht enthalten


def test_build_excerpt_drops_whole_pages_instead_of_cutting_mid_text():
    pages = ["Wohneinheit A " * 50, "Wohneinheit B " * 50, "Wohneinheit C " * 50]
    excerpt = build_excerpt(pages, ["Wohneinheit"], context_pages=1, max_chars=len(pages[0]) + 50)

    assert "[Seite 1]" in excerpt
    assert excerpt.count("[Seite") == 1  # zu lang für mehr als eine Seite -> ganze Seiten weggelassen
    assert not excerpt.rstrip().endswith("Wohnein")  # keine mitten im Wort abgeschnittene Seite


# -- Pydantic-Validierung -------------------------------------------------------------------------


def test_projekt_extraktion_defaults_to_unklar():
    p = ProjektExtraktion()
    assert p.wohnform is Wohnform.UNKLAR
    assert p.verfahrensstand is Verfahrensstand.UNKLAR
    assert p.unklar is True


def test_projekt_extraktion_not_unklar_with_number_and_wohnform():
    p = ProjektExtraktion(we_gesamt=12, wohnform=Wohnform.MIETE)
    assert p.unklar is False


def test_evidenz_over_200_chars_rejected():
    with pytest.raises(ValidationError):
        ProjektExtraktion(evidenz="x" * 201)


def test_konfidenz_out_of_range_rejected():
    with pytest.raises(ValidationError):
        ProjektExtraktion(konfidenz=1.5)


def test_quote_gefoerdert_out_of_range_rejected():
    with pytest.raises(ValidationError):
        ProjektExtraktion(quote_gefoerdert=2.0)


def test_adresse_nested_model_roundtrips():
    p = ProjektExtraktion(adresse=Adresse(strasse="Rathausallee", hausnummer="50", ort="Norderstedt"))
    restored = ProjektExtraktion.model_validate_json(p.model_dump_json())
    assert restored.adresse.strasse == "Rathausallee"


# -- Budget / Cache -------------------------------------------------------------------------------


def test_budget_enforces_per_source_limit():
    budget = LLMBudget(max_calls_per_run=100, max_calls_per_source=2)
    assert budget.reserve("q1") is True
    assert budget.reserve("q1") is True
    assert budget.reserve("q1") is False
    assert budget.reserve("q2") is True  # andere Quelle unberührt


def test_budget_enforces_run_limit_across_sources():
    budget = LLMBudget(max_calls_per_run=1, max_calls_per_source=100)
    assert budget.reserve("q1") is True
    assert budget.reserve("q2") is False


def test_cache_roundtrip(tmp_path):
    cache = ResultCache(tmp_path / "cache")
    extraktion = ProjektExtraktion(we_gesamt=8, wohnform=Wohnform.MIETE)
    cache.set("abc", extraktion)
    loaded = cache.get("abc")
    assert loaded == extraktion


def test_cache_miss_returns_none(tmp_path):
    cache = ResultCache(tmp_path / "cache")
    assert cache.get("missing") is None


def test_cache_ignores_corrupted_entry(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = ResultCache(cache_dir)
    (cache_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
    assert cache.get("broken") is None


def test_documents_cache_key_is_order_independent(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    assert documents_cache_key([a, b]) == documents_cache_key([b, a])


def test_documents_cache_key_changes_when_content_changes(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_bytes(b"AAA")
    key1 = documents_cache_key([a])
    a.write_bytes(b"BBB")
    key2 = documents_cache_key([a])
    assert key1 != key2


def test_documents_cache_key_handles_textinhalt(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_bytes(b"AAA")
    mixed = documents_cache_key([a, Textinhalt(text="etwas Text")])
    assert mixed == documents_cache_key([Textinhalt(text="etwas Text"), a])  # reihenfolgeunabhängig
    assert documents_cache_key([Textinhalt(text="etwas Text")]) != documents_cache_key([Textinhalt(text="anderer")])


# -- Fake Anthropic-Client für triage()/extract()/analyze_documents() -----------------------------


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(),
        filters=FiltersConfig(prefilter_keywords=KEYWORDS),
        llm=LLMConfig(
            triage_model="fake-triage",
            extraction_model="fake-extraction",
            max_calls_per_run=10,
            max_calls_per_source=10,
        ),
        database=DatabaseConfig(),
        dashboard=DashboardConfig(),
    )


def test_triage_parses_response(settings):
    client = FakeAnthropicClient([{"entscheidung": "JA", "begruendung": "Miete"}])
    ergebnis = triage(client, settings, "irgendein Text", "q1")
    assert ergebnis.entscheidung is TriageEntscheidung.JA
    assert client.calls[0]["model"] == "fake-triage"
    assert "temperature" not in client.calls[0]  # aktuelle API-Generation kennt keinen Temperatur-Parameter


def test_extract_parses_valid_response_on_first_try(settings):
    client = FakeAnthropicClient([{"we_gesamt": 12, "wohnform": "MIETE", "konfidenz": 0.8}])
    ergebnis = extract(client, settings, "irgendein Text", "q1")
    assert ergebnis.we_gesamt == 12
    assert len(client.calls) == 1


def test_extract_repairs_after_invalid_first_response(settings):
    client = FakeAnthropicClient(
        [
            {"konfidenz": 5.0},  # ungültig: außerhalb 0..1
            {"we_gesamt": 6, "wohnform": "GEFOERDERT", "konfidenz": 0.5},
        ]
    )
    ergebnis = extract(client, settings, "irgendein Text", "q1")
    assert ergebnis is not None
    assert ergebnis.we_gesamt == 6
    assert len(client.calls) == 2
    assert "<fehler>" in client.calls[1]["messages"][0]["content"]


def test_extract_gives_up_after_two_invalid_responses(settings):
    client = FakeAnthropicClient([{"konfidenz": 5.0}, {"konfidenz": -1.0}])
    ergebnis = extract(client, settings, "irgendein Text", "q1")
    assert ergebnis is None
    assert len(client.calls) == 2


# -- analyze_documents: die vollständige Zustandsmaschine ------------------------------------------


def _relevant_pdf(tmp_path, name="v.pdf", we=12):
    path = tmp_path / name
    path.write_bytes(_make_pdf(f"Es entstehen {we} Wohneinheiten, davon gefördert."))
    return path


def _analyze(paths, client, settings, tmp_path, budget=None, quelle_id="q1"):
    return analyze_documents(
        paths,
        client=client,
        settings=settings,
        budget=budget or LLMBudget(10, 10),
        cache=ResultCache(tmp_path / "cache"),
        quelle_id=quelle_id,
    )


def test_analyze_documents_no_paths_is_not_relevant(tmp_path, settings):
    result = _analyze([], FakeAnthropicClient([]), settings, tmp_path)
    assert result.status is AnalyseStatus.NICHT_RELEVANT


def test_analyze_documents_ocr_noetig_when_no_text(tmp_path, settings):
    path = tmp_path / "scan.pdf"
    path.write_bytes(_make_pdf(None))
    result = _analyze([path], FakeAnthropicClient([]), settings, tmp_path)
    assert result.status is AnalyseStatus.OCR_NOETIG


def test_analyze_documents_handles_corrupt_pdf_as_fehler(tmp_path, settings):
    path = tmp_path / "kaputt.pdf"
    path.write_bytes(b"%PDF-1.4\nnot a real pdf structure at all")
    result = _analyze([path], FakeAnthropicClient([]), settings, tmp_path)
    assert result.status is AnalyseStatus.FEHLER


def test_analyze_documents_not_relevant_without_keyword_hit(tmp_path, settings):
    path = tmp_path / "doc.pdf"
    path.write_bytes(_make_pdf("Es geht nur um Strassenbau."))
    result = _analyze([path], FakeAnthropicClient([]), settings, tmp_path)
    assert result.status is AnalyseStatus.NICHT_RELEVANT
    assert "Keyword" in result.grund


def test_analyze_documents_not_relevant_on_triage_nein(tmp_path, settings):
    path = _relevant_pdf(tmp_path)
    client = FakeAnthropicClient([{"entscheidung": "NEIN", "begruendung": "doch nicht"}])
    result = _analyze([path], client, settings, tmp_path)
    assert result.status is AnalyseStatus.NICHT_RELEVANT
    assert len(client.calls) == 1  # Stufe 3 wurde nicht mehr aufgerufen


def test_analyze_documents_relevant_end_to_end_and_caches(tmp_path, settings):
    path = _relevant_pdf(tmp_path)
    client = FakeAnthropicClient(
        [
            {"entscheidung": "JA"},
            {"we_gesamt": 12, "wohnform": "GEFOERDERT", "konfidenz": 0.9},
        ]
    )
    cache = ResultCache(tmp_path / "cache")
    budget = LLMBudget(10, 10)

    result = analyze_documents([path], client=client, settings=settings, budget=budget, cache=cache, quelle_id="q1")
    assert result.status is AnalyseStatus.RELEVANT
    assert result.extraktion.we_gesamt == 12
    assert result.aus_cache is False

    # zweiter Aufruf: Cache-Treffer, keine weiteren LLM-Aufrufe
    result2 = analyze_documents([path], client=client, settings=settings, budget=budget, cache=cache, quelle_id="q1")
    assert result2.status is AnalyseStatus.RELEVANT
    assert result2.aus_cache is True
    assert len(client.calls) == 2  # unverändert seit dem ersten Aufruf


def test_analyze_documents_accepts_textinhalt_without_pdf(tmp_path, settings):
    """Quellen ohne eigenes PDF (z. B. radar/sources/bv_hh.py) liefern `Textinhalt` statt eines
    Pfades - muss dieselbe Zustandsmaschine durchlaufen wie ein PDF."""
    text = Textinhalt(text="Beschlussvorlage: 7 geförderte Wohneinheiten entstehen. " * 5)
    client = FakeAnthropicClient(
        [
            {"entscheidung": "JA"},
            {"we_gesamt": 7, "wohnform": "GEFOERDERT", "konfidenz": 0.85},
        ]
    )
    result = _analyze([text], client, settings, tmp_path)
    assert result.status is AnalyseStatus.RELEVANT
    assert result.extraktion.we_gesamt == 7


def test_analyze_documents_budget_exhausted_before_triage(tmp_path, settings):
    path = _relevant_pdf(tmp_path)
    budget = LLMBudget(max_calls_per_run=0, max_calls_per_source=10)
    result = _analyze([path], FakeAnthropicClient([]), settings, tmp_path, budget=budget)
    assert result.status is AnalyseStatus.BUDGET_ERSCHOEPFT


def test_analyze_documents_error_on_repeated_invalid_extraction(tmp_path, settings):
    path = _relevant_pdf(tmp_path)
    client = FakeAnthropicClient(
        [
            {"entscheidung": "JA"},
            {"konfidenz": 5.0},
            {"konfidenz": -1.0},
        ]
    )
    result = _analyze([path], client, settings, tmp_path)
    assert result.status is AnalyseStatus.FEHLER
