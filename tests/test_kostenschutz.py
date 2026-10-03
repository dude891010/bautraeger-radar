"""Kostenschutz für die Anthropic-API: strikte Budget-Zählung je HTTP-Anfrage, begrenzte Retries,
Notbremse bei Konto-/Dauerfehlern, Fehlversuche je Vorlage über Läufe hinweg und Lauf-Sperre gegen
parallele Läufe. Kein Netzzugriff: die API wird durch Fake-Clients ersetzt."""

import os
import time
from datetime import date

import anthropic
import httpx2
import pytest

from radar import database, parser
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
from radar.parser import (
    AnalyseErgebnis,
    AnalyseStatus,
    LLMBudget,
    LLMGesperrt,
    ResultCache,
    Textinhalt,
    analyze_documents,
    extract,
    make_client,
    triage,
)
from radar.scraper import LaufLaeuftBereits, lauf_sperre, lauf_sperre_aktiv, lauf_sperre_pfad
from radar.sources.base import Sitzung
from tests.conftest import FakeAnthropicClient

RELEVANT = "18 Wohneinheiten als Mietwohnungen geplant."


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    monkeypatch.setattr(parser, "_sleep", lambda sekunden: None)  # kein echtes Warten zwischen Retries
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(),
        filters=FiltersConfig(prefilter_keywords=["Wohneinheit"]),
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


def _api_error(cls, status: int, message: str = "Fehler", headers: dict | None = None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers=headers or {})
    return cls(message, response=response, body=None)


class ScriptedClient(FakeAnthropicClient):
    """Wie FakeAnthropicClient, aber ein Eintrag, der eine Exception ist, wird geworfen statt
    beantwortet - simuliert API-Fehler in beliebiger Reihenfolge."""

    def parse(self, **kwargs):
        if self._raw_responses and isinstance(self._raw_responses[0], Exception):
            self.calls.append(kwargs)
            raise self._raw_responses.pop(0)
        return super().parse(**kwargs)


def _analyze(client, settings, budget, tmp_path, quelle_id="q1"):
    return analyze_documents(
        [Textinhalt(text=RELEVANT)],
        client=client,
        settings=settings,
        budget=budget,
        cache=ResultCache(tmp_path / "cache"),
        quelle_id=quelle_id,
    )


# -- Jede HTTP-Anfrage zählt -----------------------------------------------------------------------


def test_retries_count_against_budget(settings):
    client = ScriptedClient(
        [_api_error(anthropic.OverloadedError, 529), _api_error(anthropic.RateLimitError, 429), {"entscheidung": "JA"}]
    )
    budget = LLMBudget(10, 10)
    triage(client, settings, budget, "Text", "q1")
    assert len(client.calls) == 3
    assert budget.anfragen() == 3  # früher: 1 gezählt, bis zu 9 tatsächlich gesendet


def test_retries_stop_when_budget_runs_out(settings):
    client = ScriptedClient([_api_error(anthropic.InternalServerError, 500)] * 5)
    budget = LLMBudget(max_calls_per_run=2, max_calls_per_source=2)
    with pytest.raises(LLMGesperrt, match="pro Lauf erschöpft"):
        triage(client, settings, budget, "Text", "q1")
    assert len(client.calls) == 2


def test_transient_errors_retried_at_most_max_attempts(settings):
    client = ScriptedClient([anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))] * 10)
    budget = LLMBudget(100, 100)
    with pytest.raises(anthropic.APIConnectionError):
        triage(client, settings, budget, "Text", "q1")
    assert len(client.calls) == settings.llm.max_attempts_per_call == 3


def test_repair_attempts_count_against_budget(settings):
    client = FakeAnthropicClient([{"konfidenz": 5.0}, {"konfidenz": -1.0}])
    budget = LLMBudget(10, 10)
    assert extract(client, settings, budget, "Text", "q1") is None
    assert budget.anfragen() == 2


def test_non_transient_error_is_not_retried(settings, tmp_path):
    client = ScriptedClient([_api_error(anthropic.BadRequestError, 400, "prompt is too long")])
    budget = LLMBudget(10, 10)
    ergebnis = _analyze(client, settings, budget, tmp_path)
    assert ergebnis.status is AnalyseStatus.FEHLER
    assert len(client.calls) == 1
    assert budget.gesperrt_grund is None  # ein einzelnes Dokument-Problem sperrt nicht den ganzen Lauf


# -- Notbremse ----------------------------------------------------------------------------------------


def test_credit_balance_error_trips_emergency_brake_after_one_request(settings, tmp_path):
    fehler = _api_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the API")
    client = ScriptedClient([fehler])
    budget = LLMBudget(100, 100)

    erste = _analyze(client, settings, budget, tmp_path)
    zweite = _analyze(client, settings, budget, tmp_path, quelle_id="andere_quelle")

    assert len(client.calls) == 1  # keine einzige weitere Anfrage, auch nicht für andere Quellen
    assert "credit balance" in budget.gesperrt_grund
    # BUDGET_ERSCHOEPFT statt FEHLER: kein Fehlversuch der Vorlage, nächster Lauf versucht es erneut
    assert erste.status is AnalyseStatus.BUDGET_ERSCHOEPFT
    assert zweite.status is AnalyseStatus.BUDGET_ERSCHOEPFT
    assert "Notbremse" in zweite.grund


@pytest.mark.parametrize(
    ("cls", "status"),
    [(anthropic.AuthenticationError, 401), (anthropic.PermissionDeniedError, 403), (anthropic.NotFoundError, 404)],
)
def test_account_errors_trip_emergency_brake(settings, cls, status):
    client = ScriptedClient([_api_error(cls, status)])
    budget = LLMBudget(100, 100)
    with pytest.raises(LLMGesperrt):
        triage(client, settings, budget, "Text", "q1")
    assert len(client.calls) == 1
    assert budget.reserve("q1") is False


def test_consecutive_errors_trip_emergency_brake(settings, tmp_path):
    client = ScriptedClient([_api_error(anthropic.InternalServerError, 500)] * 50)
    budget = LLMBudget(100, 100, max_consecutive_errors=4)

    ergebnisse = [_analyze(client, settings, budget, tmp_path) for _ in range(5)]

    # Vorlage 1: 3 Versuche, Vorlage 2: 1 Versuch -> 4 Fehler in Folge -> Notbremse, danach nichts mehr
    assert len(client.calls) == 4
    assert budget.gesperrt_grund is not None
    assert [e.status for e in ergebnisse[2:]] == [AnalyseStatus.BUDGET_ERSCHOEPFT] * 3


def test_success_resets_consecutive_error_counter(settings):
    client = ScriptedClient(
        [
            _api_error(anthropic.InternalServerError, 500),
            {"entscheidung": "JA"},
            _api_error(anthropic.InternalServerError, 500),
            {"entscheidung": "JA"},
        ]
    )
    budget = LLMBudget(100, 100, max_consecutive_errors=2)
    triage(client, settings, budget, "Text", "q1")
    triage(client, settings, budget, "Text", "q1")
    assert budget.gesperrt_grund is None


def test_retry_after_header_is_respected_but_capped(settings, monkeypatch):
    wartezeiten: list[float] = []
    monkeypatch.setattr(parser, "_sleep", wartezeiten.append)
    client = ScriptedClient(
        [
            _api_error(anthropic.RateLimitError, 429, headers={"retry-after": "7"}),
            _api_error(anthropic.RateLimitError, 429, headers={"retry-after": "99999"}),
            {"entscheidung": "JA"},
        ]
    )
    triage(client, settings, LLMBudget(10, 10), "Text", "q1")
    assert wartezeiten == [7.0, 60.0]


def test_budget_summary_reports_tokens_and_brake():
    budget = LLMBudget(5, 5)
    budget.reserve("q1")
    budget.erfolg(type("Usage", (), {"input_tokens": 100, "output_tokens": 20})())
    budget.sperren("Guthaben leer")
    text = budget.zusammenfassung()
    assert "1/5 LLM-Anfragen" in text
    assert "100 input_tokens" in text
    assert "NOTBREMSE: Guthaben leer" in text


# -- Client und Konfiguration -------------------------------------------------------------------------


def test_make_client_disables_sdk_retries_and_sets_timeout(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = make_client(settings)
    assert client.max_retries == 0
    assert client.timeout == settings.llm.request_timeout_seconds


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_calls_per_run": 5000},  # Tippfehler über der harten Obergrenze
        {"max_calls_per_run": -1},
        {"max_calls_per_run": 10, "max_calls_per_source": 20},
        {"max_attempts_per_call": 0},
        {"max_attempts_per_call": 10},
        {"max_consecutive_errors": 0},
        {"request_timeout_seconds": 0},
        {"max_fehlversuche_pro_vorlage": 0},
    ],
)
def test_llm_config_rejects_unsafe_values(kwargs):
    with pytest.raises(ValueError):
        LLMConfig(**kwargs)


# -- Fehlversuche je Vorlage über Läufe hinweg ----------------------------------------------------------


def _vorlage_mit_status(conn, status: AnalyseStatus, wiederholungen: int, max_fehlversuche: int = 3) -> bool:
    database.ensure_quelle(
        conn, Source(id="q1", name="Q", bundesland="SH", system="sessionnet", base_url="https://example.org/")
    )
    sitzung_id = database.upsert_sitzung(
        conn, "q1", Sitzung(source_id="q1", external_id="s1", gremium="Bauausschuss", datum=date(2026, 9, 1))
    )
    braucht = True
    for _ in range(wiederholungen):
        vorlage_id, braucht = database.upsert_vorlage(
            conn, "q1", sitzung_id, "v1", None, "hash", max_fehlversuche=max_fehlversuche
        )
        if not braucht:
            return False
        database.speichere_analyse_ergebnis(
            conn, vorlage_id=vorlage_id, quelle_id="q1", bundesland="SH", kommune="Q",
            ergebnis=AnalyseErgebnis(status=status, grund="test"),
        )
    _, braucht = database.upsert_vorlage(conn, "q1", sitzung_id, "v1", None, "hash", max_fehlversuche=max_fehlversuche)
    return braucht


def test_vorlage_given_up_after_max_failed_attempts(settings):
    conn = database.connect(settings)
    try:
        assert _vorlage_mit_status(conn, AnalyseStatus.FEHLER, wiederholungen=3) is False
        row = conn.execute("SELECT analyse_fehlversuche FROM vorlagen").fetchone()
        assert row["analyse_fehlversuche"] == 3
    finally:
        conn.close()


def test_vorlage_still_retried_below_max_failed_attempts(settings):
    conn = database.connect(settings)
    try:
        assert _vorlage_mit_status(conn, AnalyseStatus.FEHLER, wiederholungen=2) is True
    finally:
        conn.close()


def test_budget_exhaustion_does_not_count_as_failed_attempt(settings):
    conn = database.connect(settings)
    try:
        assert _vorlage_mit_status(conn, AnalyseStatus.BUDGET_ERSCHOEPFT, wiederholungen=10) is True
        row = conn.execute("SELECT analyse_fehlversuche FROM vorlagen").fetchone()
        assert row["analyse_fehlversuche"] == 0
    finally:
        conn.close()


# -- Lauf-Sperre gegen parallele Läufe -----------------------------------------------------------------


def test_second_run_is_rejected_while_first_is_active(settings):
    with lauf_sperre(settings):
        assert lauf_sperre_aktiv(settings)
        with pytest.raises(LaufLaeuftBereits):
            with lauf_sperre(settings):
                pass
    assert not lauf_sperre_aktiv(settings)  # nach dem Lauf wieder frei


def test_lock_is_released_after_exception(settings):
    with pytest.raises(RuntimeError):
        with lauf_sperre(settings):
            raise RuntimeError("Lauf abgestürzt")
    assert not lauf_sperre_pfad(settings).exists()


def test_stale_lock_from_crashed_run_is_taken_over(settings):
    pfad = lauf_sperre_pfad(settings)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("pid=1 start=gestern\n", encoding="utf-8")
    alt = time.time() - 13 * 3600
    os.utime(pfad, (alt, alt))

    with lauf_sperre(settings):
        assert "start=gestern" not in pfad.read_text(encoding="utf-8")
