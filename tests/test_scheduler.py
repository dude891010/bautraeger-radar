"""Tests für radar/scheduler.py: Berechnung des nächsten Laufzeitpunkts und die Lauf-Schleife.
Kein echtes `time.sleep`/`datetime.now()` - eine Fake-Uhr macht die Schleife ohne Wartezeit und
ohne Netzzugriff testbar (siehe CLAUDE.md: "Netzwerk ... hinter dünnen Schnittstellen")."""

from datetime import datetime, timedelta

import pytest

from radar.config import (
    DashboardConfig,
    DatabaseConfig,
    FiltersConfig,
    LLMConfig,
    ScanConfig,
    SchedulerConfig,
    ScraperConfig,
    Settings,
)
from radar.notifier import NeuerTreffer
from radar.scheduler import _next_run, run_forever
from radar.scraper import SourceRunResult


def test_next_run_this_week_when_target_still_ahead():
    montag_morgens = datetime(2026, 9, 14, 8, 0)  # ein Montag
    ziel = _next_run(montag_morgens, weekday=2, hour=3, minute=0)  # Mittwoch 03:00
    assert ziel == datetime(2026, 9, 16, 3, 0)


def test_next_run_next_week_when_target_already_passed():
    donnerstag = datetime(2026, 9, 17, 12, 0)
    ziel = _next_run(donnerstag, weekday=2, hour=3, minute=0)  # Mittwoch liegt diese Woche schon hinter uns
    assert ziel == datetime(2026, 9, 23, 3, 0)


def test_next_run_skips_to_next_week_when_now_equals_target():
    genau_jetzt = datetime(2026, 9, 16, 3, 0)
    ziel = _next_run(genau_jetzt, weekday=2, hour=3, minute=0)
    assert ziel == datetime(2026, 9, 23, 3, 0)


class _FakeClock:
    """`now()` liefert den aktuellen simulierten Zeitpunkt, `sleep(s)` spult ihn vor - keine
    echte Wartezeit im Test."""

    def __init__(self, start: datetime):
        self.value = start

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(),
        filters=FiltersConfig(),
        llm=LLMConfig(),
        database=DatabaseConfig(),
        dashboard=DashboardConfig(),
        scheduler=SchedulerConfig(weekday=0, hour=3, minute=0, check_interval_seconds=3600),
    )


def test_run_forever_waits_then_runs_exactly_max_runs_times(monkeypatch, settings):
    clock = _FakeClock(datetime(2026, 9, 14, 8, 0))  # Montag, nach der geplanten Uhrzeit
    calls = []

    def fake_run_full(s):
        calls.append(s)
        return []

    monkeypatch.setattr("radar.scheduler.run_full", fake_run_full)
    monkeypatch.setattr("radar.scheduler.send_notification", lambda treffer: False)

    run_forever(settings, max_runs=2, sleep=clock.sleep, now=clock.now)

    assert len(calls) == 2
    assert calls[0] is settings
    # zwischen den zwei Läufen muss mindestens eine Woche simulierter Zeit vergangen sein
    assert clock.value >= datetime(2026, 9, 14, 8, 0) + timedelta(days=7)


def test_run_forever_isolates_failed_run(monkeypatch, settings):
    clock = _FakeClock(datetime(2026, 9, 14, 8, 0))
    calls = []

    def boom(s):
        calls.append(s)
        if len(calls) == 1:
            raise RuntimeError("kaputt")
        return []

    monkeypatch.setattr("radar.scheduler.run_full", boom)
    monkeypatch.setattr("radar.scheduler.send_notification", lambda treffer: False)

    run_forever(settings, max_runs=2, sleep=clock.sleep, now=clock.now)  # darf nicht propagieren

    assert len(calls) == 2


def test_run_forever_notifies_with_flattened_neue_treffer_across_sources(monkeypatch, settings):
    """radar/notifier.py bekommt die `neue_treffer` aller Quellen zusammen, nicht pro Quelle
    einzeln (eine E-Mail pro Lauf, CLAUDE.md: "optional E-Mail bei neuen Treffern")."""
    clock = _FakeClock(datetime(2026, 9, 14, 8, 0))
    treffer_a = NeuerTreffer(kommune="A-Stadt", titel="Projekt A", we_gesamt=10, url="https://a.example.org")
    treffer_b = NeuerTreffer(kommune="B-Stadt", titel="Projekt B", we_gesamt=20, url="https://b.example.org")
    results = [
        SourceRunResult(source_id="a", neue_treffer=[treffer_a]),
        SourceRunResult(source_id="b", neue_treffer=[treffer_b]),
    ]
    monkeypatch.setattr("radar.scheduler.run_full", lambda s: results)
    gesendet_mit = []
    monkeypatch.setattr("radar.scheduler.send_notification", lambda treffer: gesendet_mit.append(treffer) or True)

    run_forever(settings, max_runs=1, sleep=clock.sleep, now=clock.now)

    assert gesendet_mit == [[treffer_a, treffer_b]]
