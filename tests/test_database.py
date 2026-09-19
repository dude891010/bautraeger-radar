"""Tests für radar/database.py: Schema/Migration, Quellen-Monitoring, Sitzungen/Vorlagen/Dokumente,
Projekt-Dedup und Statuspflege. Reines SQLite, kein Netzzugriff, `tmp_path` pro Test."""

import sqlite3
from dataclasses import replace
from datetime import date, datetime

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
from radar.parser import Adresse, AnalyseErgebnis, AnalyseStatus, ProjektExtraktion, Verfahrensstand, Wohnform
from radar.scraper import SourceRunResult
from radar.sources.base import Dokument, Sitzung


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(),
        filters=FiltersConfig(),
        llm=LLMConfig(),
        database=DatabaseConfig(path=str(tmp_path / "radar.db")),
        dashboard=DashboardConfig(),
    )


@pytest.fixture
def conn(settings):
    connection = database.connect(settings)
    yield connection
    connection.close()


@pytest.fixture
def source():
    return Source(
        id="test", name="Testquelle", bundesland="SH", system="sessionnet", base_url="https://example.org/"
    )


@pytest.fixture
def seeded_conn(conn, source):
    """Eine `quellen`-Zeile anlegen (Fremdschlüssel-Voraussetzung für sitzungen/vorlagen/projekte)."""
    database.record_source_run(conn, source, SourceRunResult(source_id="test"))
    return conn


def _sitzung(external_id="s1", gremium="Ausschuss für Stadtentwicklung und Verkehr"):
    return Sitzung(
        source_id="test", external_id=external_id, gremium=gremium, datum=date(2026, 9, 3), url="si0057.php?x"
    )


def _dokument(external_id="d1", titel="Anlage 1"):
    return Dokument(
        source_id="test",
        external_id=external_id,
        titel=titel,
        url="getfile.php?id=1",
        sitzung_external_id="s1",
        typ="anlage",
    )


def _extraktion(**overrides):
    defaults = dict(
        projektbezeichnung="Wohnpark Ochsenzoll",
        adresse=Adresse(strasse="Ochsenzollstraße", hausnummer="12", ort="Norderstedt"),
        we_gesamt=18,
        we_miete=18,
        wohnform=Wohnform.MIETE,
        verfahrensstand=Verfahrensstand.BAUGENEHMIGUNG,
        konfidenz=0.9,
    )
    defaults.update(overrides)
    return ProjektExtraktion(**defaults)


def _upsert_projekt(conn, *, quelle_id="test", bundesland="SH", kommune="Norderstedt", extraktion=None, **overrides):
    """Liefert nur die Projekt-ID (das `ist_neu`-Flag testen die dedizierten Tests unten)."""
    extraktion = extraktion or _extraktion(**overrides)
    projekt_id, _ = database.upsert_projekt(
        conn, quelle_id=quelle_id, bundesland=bundesland, kommune=kommune, extraktion=extraktion
    )
    return projekt_id


# -- Schema / Migration ---------------------------------------------------------------------------


def test_connect_creates_file_and_applies_schema(settings):
    conn = database.connect(settings)
    try:
        assert settings.resolve_path(settings.database.path).exists()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"quellen", "sitzungen", "vorlagen", "dokumente", "projekte", "projekt_vorlage", "status_log"} <= tables
    finally:
        conn.close()


def test_connect_sets_wal_and_foreign_keys(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_sets_busy_timeout(conn):
    # 5000 ms: Dashboard-Subprozess und Scheduler-Dienst greifen auf dieselbe Datei zu (siehe
    # docker-compose.yml), busy_timeout lässt einen kollidierenden Schreibzugriff kurz warten
    # statt sofort "database is locked" zu werfen.
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connect_twice_is_idempotent(settings):
    database.connect(settings).close()
    conn2 = database.connect(settings)
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn2.close()


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sitzungen (quelle_id, external_id, gremium, datum) VALUES ('fehlt', 'x', 'g', '2026-09-01')"
        )


# -- Quellen-Monitoring ---------------------------------------------------------------------------


def test_ensure_quelle_seeds_row_without_touching_monitoring_fields(conn, source):
    database.ensure_quelle(conn, source)
    row = conn.execute("SELECT * FROM quellen WHERE id = 'test'").fetchone()
    assert row["name"] == "Testquelle"
    assert row["letzter_erfolg"] is None
    assert row["letzter_fehler"] is None


def test_ensure_quelle_is_idempotent(conn, source):
    database.ensure_quelle(conn, source)
    database.ensure_quelle(conn, source)
    assert conn.execute("SELECT COUNT(*) FROM quellen").fetchone()[0] == 1


def test_record_source_run_success_sets_erfolg(conn, source):
    when = datetime(2026, 9, 19, 10, 0)
    database.record_source_run(conn, source, SourceRunResult(source_id="test", sitzungen=5), when=when)
    row = conn.execute("SELECT * FROM quellen WHERE id = 'test'").fetchone()
    assert row["letzter_erfolg"] == when.isoformat()
    assert row["letzter_fehler"] is None
    assert row["anzahl_sitzungen"] == 5


def test_record_source_run_preserves_previous_success_on_failure(conn, source):
    t1 = datetime(2026, 9, 1, 10, 0)
    t2 = datetime(2026, 9, 8, 10, 0)
    database.record_source_run(conn, source, SourceRunResult(source_id="test"), when=t1)
    database.record_source_run(conn, source, SourceRunResult(source_id="test", fehler=["kaputt"]), when=t2)

    row = conn.execute("SELECT * FROM quellen WHERE id = 'test'").fetchone()
    assert row["letzter_erfolg"] == t1.isoformat()  # bleibt erhalten
    assert row["letzter_fehler"] == t2.isoformat()
    assert row["letzter_fehler_text"] == "kaputt"


def test_record_source_run_preserves_previous_failure_on_success(conn, source):
    t1 = datetime(2026, 9, 1, 10, 0)
    t2 = datetime(2026, 9, 8, 10, 0)
    database.record_source_run(conn, source, SourceRunResult(source_id="test", fehler=["kaputt"]), when=t1)
    database.record_source_run(conn, source, SourceRunResult(source_id="test"), when=t2)

    row = conn.execute("SELECT * FROM quellen WHERE id = 'test'").fetchone()
    assert row["letzter_fehler"] == t1.isoformat()  # bleibt erhalten
    assert row["letzter_erfolg"] == t2.isoformat()


# -- Sitzungen / Dokumente ------------------------------------------------------------------------


def test_upsert_sitzung_is_idempotent_and_updates_fields(seeded_conn):
    id1 = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    updated = replace(_sitzung(), uhrzeit="18:30 Uhr")
    id2 = database.upsert_sitzung(seeded_conn, "test", updated)

    assert id1 == id2
    row = seeded_conn.execute("SELECT * FROM sitzungen WHERE id = ?", (id1,)).fetchone()
    assert row["uhrzeit"] == "18:30 Uhr"
    assert seeded_conn.execute("SELECT COUNT(*) FROM sitzungen").fetchone()[0] == 1


def test_upsert_dokument_is_idempotent(seeded_conn):
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    id1 = database.upsert_dokument(seeded_conn, "test", sitzung_id, None, _dokument())
    id2 = database.upsert_dokument(seeded_conn, "test", sitzung_id, None, _dokument(titel="Neuer Titel"))

    assert id1 == id2
    row = seeded_conn.execute("SELECT * FROM dokumente WHERE id = ?", (id1,)).fetchone()
    assert row["titel"] == "Neuer Titel"


def test_mark_dokument_heruntergeladen(seeded_conn):
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    dok_id = database.upsert_dokument(seeded_conn, "test", sitzung_id, None, _dokument())
    database.mark_dokument_heruntergeladen(seeded_conn, dok_id, "abc123", "data/raw/test/d1.pdf")

    row = seeded_conn.execute("SELECT * FROM dokumente WHERE id = ?", (dok_id,)).fetchone()
    assert row["sha256"] == "abc123"
    assert row["dateipfad"] == "data/raw/test/d1.pdf"


# -- Vorlagen (Dedup Stufe 1) ---------------------------------------------------------------------


def test_upsert_vorlage_unchanged_hash_still_new_row_but_unsettled_needs_analysis(seeded_conn):
    """Noch nie analysiert ('neu') -> auch beim zweiten Aufruf mit demselben Hash soll der
    Aufrufer die Analyse (erneut) versuchen, es entsteht aber keine zweite Zeile."""
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    id1, braucht1 = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")
    id2, braucht2 = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")

    assert braucht1 is True
    assert braucht2 is True
    assert id1 == id2
    assert seeded_conn.execute("SELECT COUNT(*) FROM vorlagen").fetchone()[0] == 1


def test_upsert_vorlage_unchanged_hash_after_settled_analysis_skips(seeded_conn):
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    id1, _ = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")
    seeded_conn.execute("UPDATE vorlagen SET analyse_status = 'RELEVANT' WHERE id = ?", (id1,))

    id2, braucht2 = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")
    assert id1 == id2
    assert braucht2 is False


def test_upsert_vorlage_unchanged_hash_after_budget_erschoepft_retries(seeded_conn):
    """Ein voriger Versuch, der nur am Budget scheiterte, muss beim nächsten Lauf erneut
    versucht werden - sonst würde eine Vorlage nie mehr analysiert."""
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    id1, _ = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")
    seeded_conn.execute("UPDATE vorlagen SET analyse_status = 'BUDGET_ERSCHOEPFT' WHERE id = ?", (id1,))

    id2, braucht2 = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")
    assert id1 == id2
    assert braucht2 is True


def test_upsert_vorlage_changed_hash_creates_new_row(seeded_conn):
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    id1, _ = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")
    id2, neu2 = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-b")

    assert neu2 is True
    assert id1 != id2  # geänderte Anlage -> neuer Datensatz, erneut zu analysieren
    assert seeded_conn.execute("SELECT COUNT(*) FROM vorlagen").fetchone()[0] == 2


# -- normalize_projekt_key -------------------------------------------------------------------------


def test_normalize_projekt_key_is_case_and_whitespace_insensitive():
    a = _extraktion(adresse=Adresse(strasse="Ochsenzollstraße", hausnummer="12", ort="Norderstedt"))
    b = _extraktion(adresse=Adresse(strasse="  ochsenzollstraße", hausnummer="12", ort="norderstedt  "))
    assert database.normalize_projekt_key(a) == database.normalize_projekt_key(b)


def test_normalize_projekt_key_differs_for_different_addresses():
    a = _extraktion(adresse=Adresse(strasse="Ochsenzollstraße", hausnummer="12", ort="Norderstedt"))
    b = _extraktion(adresse=Adresse(strasse="Falkenbergstraße", hausnummer="8", ort="Norderstedt"))
    assert database.normalize_projekt_key(a) != database.normalize_projekt_key(b)


def test_normalize_projekt_key_falls_back_to_bezeichnung_without_address():
    a = _extraktion(adresse=Adresse(), projektbezeichnung="Wohnpark Ochsenzoll")
    assert "wohnpark ochsenzoll" in database.normalize_projekt_key(a)


def test_normalize_projekt_key_never_collides_when_fully_empty():
    a = ProjektExtraktion()
    b = ProjektExtraktion()
    assert database.normalize_projekt_key(a) != database.normalize_projekt_key(b)


# -- Projekte (Dedup Stufe 2) ---------------------------------------------------------------------


def test_upsert_projekt_creates_with_status_neu(seeded_conn):
    projekt_id = _upsert_projekt(seeded_conn)
    row = seeded_conn.execute("SELECT * FROM projekte WHERE id = ?", (projekt_id,)).fetchone()
    assert row["status"] == "Neu"
    assert row["we_gesamt"] == 18
    log = seeded_conn.execute("SELECT * FROM status_log WHERE projekt_id = ?", (projekt_id,)).fetchall()
    assert len(log) == 1
    assert log[0]["neuer_status"] == "Neu"


def test_upsert_projekt_same_address_updates_not_duplicates(seeded_conn):
    id1 = _upsert_projekt(seeded_conn, we_gesamt=18)
    id2 = _upsert_projekt(seeded_conn, we_gesamt=20, verfahrensstand=Verfahrensstand.SATZUNGSBESCHLUSS)

    assert id1 == id2
    assert seeded_conn.execute("SELECT COUNT(*) FROM projekte").fetchone()[0] == 1
    row = seeded_conn.execute("SELECT * FROM projekte WHERE id = ?", (id1,)).fetchone()
    assert row["we_gesamt"] == 20  # neuere Extraktion aktualisiert die Zahl
    assert row["verfahrensstand"] == "SATZUNGSBESCHLUSS"


def test_upsert_projekt_ist_neu_only_true_on_first_insert(seeded_conn):
    """`ist_neu` speist die E-Mail-Benachrichtigung (radar/notifier.py) - nur eine echte
    Neuanlage soll gemeldet werden, keine bloße Aktualisierung bekannter Zahlen."""
    _, ist_neu1 = database.upsert_projekt(
        seeded_conn, quelle_id="test", bundesland="SH", kommune="Norderstedt", extraktion=_extraktion(we_gesamt=18)
    )
    _, ist_neu2 = database.upsert_projekt(
        seeded_conn, quelle_id="test", bundesland="SH", kommune="Norderstedt", extraktion=_extraktion(we_gesamt=20)
    )

    assert ist_neu1 is True
    assert ist_neu2 is False


def test_upsert_projekt_never_overwrites_manual_status_or_notizen(seeded_conn):
    projekt_id = _upsert_projekt(seeded_conn)
    database.set_projekt_status(seeded_conn, projekt_id, "In Prüfung")
    database.set_projekt_notizen(seeded_conn, projekt_id, "Vertrieb informiert")

    _upsert_projekt(seeded_conn, we_gesamt=99)

    row = seeded_conn.execute("SELECT * FROM projekte WHERE id = ?", (projekt_id,)).fetchone()
    assert row["status"] == "In Prüfung"
    assert row["notizen"] == "Vertrieb informiert"
    assert row["we_gesamt"] == 99  # extrahierte Felder werden trotzdem aktualisiert


def test_different_quelle_does_not_dedup_same_address(seeded_conn, conn):
    other = Source(id="other", name="Andere Quelle", bundesland="HH", system="sessionnet", base_url="https://example.org/")
    database.record_source_run(conn, other, SourceRunResult(source_id="other"))

    id1 = _upsert_projekt(seeded_conn, quelle_id="test", bundesland="SH", kommune="Norderstedt")
    id2 = _upsert_projekt(seeded_conn, quelle_id="other", bundesland="HH", kommune="Andere Stadt")
    assert id1 != id2


def test_set_projekt_status_unknown_projekt_raises(conn):
    with pytest.raises(ValueError):
        database.set_projekt_status(conn, 999, "In Prüfung")


def test_set_projekt_status_logs_transition(seeded_conn):
    projekt_id = _upsert_projekt(seeded_conn)
    database.set_projekt_status(seeded_conn, projekt_id, "In Prüfung", notiz="Erstkontakt")
    database.set_projekt_status(seeded_conn, projekt_id, "Archiviert", notiz="kein Interesse")

    log = seeded_conn.execute("SELECT * FROM status_log WHERE projekt_id = ? ORDER BY id", (projekt_id,)).fetchall()
    assert [row["neuer_status"] for row in log] == ["Neu", "In Prüfung", "Archiviert"]
    assert log[1]["alter_status"] == "Neu"
    assert log[2]["alter_status"] == "In Prüfung"
    assert log[2]["notiz"] == "kein Interesse"


# -- speichere_analyse_ergebnis ---------------------------------------------------------------------


def test_speichere_analyse_ergebnis_not_relevant_creates_no_project(seeded_conn):
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    vorlage_id, _ = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", None, "hash-a")

    ergebnis = AnalyseErgebnis(status=AnalyseStatus.NICHT_RELEVANT, grund="kein Treffer")
    projekt_id = database.speichere_analyse_ergebnis(
        seeded_conn, vorlage_id=vorlage_id, quelle_id="test", bundesland="SH", kommune="Norderstedt", ergebnis=ergebnis
    )

    assert projekt_id is None
    row = seeded_conn.execute("SELECT analyse_status FROM vorlagen WHERE id = ?", (vorlage_id,)).fetchone()
    assert row["analyse_status"] == "NICHT_RELEVANT"
    assert seeded_conn.execute("SELECT COUNT(*) FROM projekte").fetchone()[0] == 0


def test_speichere_analyse_ergebnis_relevant_links_project_and_vorlage(seeded_conn):
    sitzung_id = database.upsert_sitzung(seeded_conn, "test", _sitzung())
    vorlage_id, _ = database.upsert_vorlage(seeded_conn, "test", sitzung_id, "v1", "A 26/0001", "hash-a")

    ergebnis = AnalyseErgebnis(status=AnalyseStatus.RELEVANT, extraktion=_extraktion())
    projekt_id, ist_neu = database.speichere_analyse_ergebnis(
        seeded_conn, vorlage_id=vorlage_id, quelle_id="test", bundesland="SH", kommune="Norderstedt", ergebnis=ergebnis
    )

    assert projekt_id is not None
    assert ist_neu is True
    link = seeded_conn.execute(
        "SELECT * FROM projekt_vorlage WHERE projekt_id = ? AND vorlage_id = ?", (projekt_id, vorlage_id)
    ).fetchone()
    assert link is not None


def test_project_appears_in_multiple_sessions(seeded_conn):
    """CLAUDE.md: ein Projekt taucht in mehreren Gremien und Sitzungen auf."""
    sitzung1_id = database.upsert_sitzung(seeded_conn, "test", _sitzung(external_id="s1"))
    andere_sitzung = Sitzung(source_id="test", external_id="s2", gremium="Hauptausschuss", datum=date(2026, 10, 1))
    sitzung2_id = database.upsert_sitzung(seeded_conn, "test", andere_sitzung)
    vorlage1_id, _ = database.upsert_vorlage(seeded_conn, "test", sitzung1_id, "v1", "A 26/0001", "hash-a")
    vorlage2_id, _ = database.upsert_vorlage(seeded_conn, "test", sitzung2_id, "v2", "A 26/0099", "hash-b")

    ergebnis = AnalyseErgebnis(status=AnalyseStatus.RELEVANT, extraktion=_extraktion())
    projekt_id1, ist_neu1 = database.speichere_analyse_ergebnis(
        seeded_conn, vorlage_id=vorlage1_id, quelle_id="test", bundesland="SH", kommune="Norderstedt", ergebnis=ergebnis
    )
    projekt_id2, ist_neu2 = database.speichere_analyse_ergebnis(
        seeded_conn, vorlage_id=vorlage2_id, quelle_id="test", bundesland="SH", kommune="Norderstedt", ergebnis=ergebnis
    )

    assert projekt_id1 == projekt_id2  # dieselbe Adresse -> dasselbe Projekt
    assert ist_neu1 is True
    assert ist_neu2 is False  # zweites Vorkommen desselben Projekts, keine Neuanlage
    vorkommen = database.list_vorlagen_fuer_projekt(seeded_conn, projekt_id1)
    assert {row["gremium"] for row in vorkommen} == {"Ausschuss für Stadtentwicklung und Verkehr", "Hauptausschuss"}


# -- Abfragen ---------------------------------------------------------------------------------------


def test_list_projekte_filters_by_status(seeded_conn):
    id1 = _upsert_projekt(seeded_conn)
    id2 = _upsert_projekt(seeded_conn, adresse=Adresse(strasse="Andere Straße", hausnummer="1", ort="Norderstedt"))
    database.set_projekt_status(seeded_conn, id2, "Archiviert")

    neu = database.list_projekte(seeded_conn, status="Neu")
    assert [row["id"] for row in neu] == [id1]
    alle = database.list_projekte(seeded_conn)
    assert {row["id"] for row in alle} == {id1, id2}


def test_list_quellen(seeded_conn):
    rows = database.list_quellen(seeded_conn)
    assert [row["id"] for row in rows] == ["test"]
