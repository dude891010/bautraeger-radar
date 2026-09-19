"""Tests für radar/cli.py: Befehle werden auf die richtige Funktion abgebildet, kein Netzzugriff
(radar.scraper.run_full/radar.scheduler.run_forever/radar.notifier.send_notification werden
gemockt)."""

import pytest

from radar.cli import main
from radar.notifier import NeuerTreffer, SMTPConfig
from radar.scraper import SourceRunResult


def test_scrape_returns_zero_when_all_sources_ok(monkeypatch):
    monkeypatch.setattr("radar.cli.run_full", lambda: [SourceRunResult(source_id="a")])
    assert main(["scrape"]) == 0


def test_scrape_returns_nonzero_when_a_source_failed(monkeypatch):
    kaputt = SourceRunResult(source_id="a", fehler=["etwas ging schief"])
    monkeypatch.setattr("radar.cli.run_full", lambda: [kaputt])
    assert main(["scrape"]) == 1


def test_scrape_without_notify_flag_never_calls_send_notification(monkeypatch):
    """Standardverhalten: ein normaler Testlauf (`python -m radar scrape`, kein --notify) darf
    nie eine E-Mail auslösen, unabhängig davon, ob neue Treffer da sind."""
    treffer = NeuerTreffer(kommune="Norderstedt", titel="X", we_gesamt=18, url=None)
    monkeypatch.setattr("radar.cli.run_full", lambda: [SourceRunResult(source_id="a", neue_treffer=[treffer])])
    calls = []
    monkeypatch.setattr("radar.cli.send_notification", lambda t: calls.append(t))
    assert main(["scrape"]) == 0
    assert calls == []


def test_scrape_with_notify_flag_sends_flattened_neue_treffer(monkeypatch):
    treffer_a = NeuerTreffer(kommune="A-Stadt", titel="A", we_gesamt=10, url=None)
    treffer_b = NeuerTreffer(kommune="B-Stadt", titel="B", we_gesamt=20, url=None)
    results = [
        SourceRunResult(source_id="a", neue_treffer=[treffer_a]),
        SourceRunResult(source_id="b", neue_treffer=[treffer_b]),
    ]
    monkeypatch.setattr("radar.cli.run_full", lambda: results)
    calls = []
    monkeypatch.setattr("radar.cli.send_notification", lambda t: calls.append(t))

    assert main(["scrape", "--notify"]) == 0

    assert calls == [[treffer_a, treffer_b]]


def test_scheduler_delegates_to_run_forever(monkeypatch):
    calls = []
    monkeypatch.setattr("radar.cli.run_forever", lambda: calls.append(True))
    assert main(["scheduler"]) == 0
    assert calls == [True]


def _config():
    return SMTPConfig(
        host="smtp.example.org",
        port=587,
        user="bot@example.org",
        password="secret",
        recipients=("vertrieb@example.org",),
    )


def test_test_email_returns_nonzero_when_smtp_not_configured(monkeypatch, capsys):
    monkeypatch.setattr("radar.cli.load_smtp_config", lambda: None)
    assert main(["test-email"]) == 1
    assert "nicht konfiguriert" in capsys.readouterr().out


def test_test_email_returns_zero_when_sent(monkeypatch, capsys):
    monkeypatch.setattr("radar.cli.load_smtp_config", lambda: _config())
    monkeypatch.setattr("radar.cli.send_notification", lambda treffer, config: True)
    assert main(["test-email"]) == 0
    assert "vertrieb@example.org" in capsys.readouterr().out


def test_test_email_returns_nonzero_when_send_fails(monkeypatch, capsys):
    monkeypatch.setattr("radar.cli.load_smtp_config", lambda: _config())
    monkeypatch.setattr("radar.cli.send_notification", lambda treffer, config: False)
    assert main(["test-email"]) == 1
    assert "fehlgeschlagen" in capsys.readouterr().out


def test_missing_command_exits_nonzero():
    with pytest.raises(SystemExit):
        main([])


def test_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit):
        main(["irgendwas"])
