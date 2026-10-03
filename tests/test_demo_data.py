"""Tests für radar/demo_data.py: Demo-Seed-Datenbank für den Dashboard-"Demo-Daten anzeigen"-
Schalter. Reines SQLite, kein Netzzugriff, `tmp_path` pro Test (wie tests/test_database.py)."""

from contextlib import closing

import pytest

from radar import database, demo_data
from radar.config import (
    DashboardConfig,
    DatabaseConfig,
    FiltersConfig,
    LLMConfig,
    ScanConfig,
    ScraperConfig,
    Settings,
)


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


def test_demo_settings_points_at_separate_db_file(settings):
    demo = demo_data.demo_settings(settings)
    assert demo.database.path != settings.database.path
    assert demo.database.path.endswith("demo.db")


def test_demo_db_path_stays_next_to_given_settings_database_not_hardcoded_project_root(settings, tmp_path):
    """Regression: `demo_db_path()` resolvte früher immer `PROJECT_ROOT / "data/demo/demo.db"`,
    unabhängig von der übergebenen `settings.database.path` - Tests mit einer `tmp_path`-Settings-
    Instanz liefen dadurch unbemerkt gegen die echte Demo-Datei im Projektverzeichnis statt gegen
    eine isolierte Kopie und häuften dort bei jedem Testlauf Daten an (sichtbar als Dutzende
    überflüssiger status_log-Einträge im Dashboard-Statusverlauf)."""
    pfad = demo_data.demo_db_path(settings)
    assert tmp_path in pfad.parents


def test_seed_populates_projects_and_quellen(settings):
    demo = demo_data.demo_settings(settings)
    with closing(database.connect(demo)) as conn:
        demo_data.seed(conn)
        projekte = database.list_projekte(conn)
        quellen = database.list_quellen(conn)

    assert 5 <= len(projekte) <= 10
    assert len(quellen) >= 1
    # We-Zahlen im vom Nutzer gewünschten Bereich (12-85)
    we_werte = [p["we_gesamt"] for p in projekte if p["we_gesamt"] is not None]
    assert all(12 <= we <= 85 for we in we_werte)
    # Mindestens ein bewusst unklarer Fall (CLAUDE.md: "unklare Fälle nie stillschweigend verwerfen")
    assert any(p["wohnform"] == "UNKLAR" for p in projekte)


def test_seed_is_idempotent(settings):
    demo = demo_data.demo_settings(settings)
    with closing(database.connect(demo)) as conn:
        demo_data.seed(conn)
        anzahl_erst = len(database.list_projekte(conn))
        demo_data.seed(conn)
        anzahl_danach = len(database.list_projekte(conn))

    assert anzahl_erst == anzahl_danach


def test_ensure_demo_seeded_seeds_only_once(settings):
    demo1 = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo1)) as conn:
        projekt_id = database.list_projekte(conn)[0]["id"]
        database.set_projekt_status(conn, projekt_id, "Archiviert")

    # Zweiter Aufruf darf die manuell gesetzte Status-Änderung nicht zurücksetzen (Demo-DB ist
    # nicht mehr leer -> seed() läuft nicht erneut).
    demo2 = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo2)) as conn:
        row = next(p for p in database.list_projekte(conn) if p["id"] == projekt_id)
        assert row["status"] == "Archiviert"


def test_reset_demo_restores_edited_status_and_clears_notizen(settings):
    """Sidebar-Button "Demo-Daten zurücksetzen": anders als `ensure_demo_seeded()` muss
    `reset_demo()` auch bei einer bereits gefüllten Demo-DB laufen und im Dashboard vorgenommene
    Demo-Bearbeitungen (Status UND Notizen) wieder auf die kanonischen Werte zurücksetzen."""
    demo1 = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo1)) as conn:
        projekt = database.list_projekte(conn)[0]
        projekt_id, urspruenglicher_status = projekt["id"], projekt["status"]
        database.set_projekt_status(conn, projekt_id, "Archiviert")
        database.set_projekt_notizen(conn, projekt_id, "Testnotiz")

    demo2 = demo_data.reset_demo(settings)
    with closing(database.connect(demo2)) as conn:
        row = next(p for p in database.list_projekte(conn) if p["id"] == projekt_id)
        assert row["status"] == urspruenglicher_status
        assert row["notizen"] == ""


def test_reset_demo_does_not_duplicate_projects(settings):
    demo1 = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo1)) as conn:
        anzahl_vorher = len(database.list_projekte(conn))

    demo2 = demo_data.reset_demo(settings)
    with closing(database.connect(demo2)) as conn:
        anzahl_nachher = len(database.list_projekte(conn))

    assert anzahl_vorher == anzahl_nachher


def test_seed_does_not_grow_status_log_on_repeated_calls(settings):
    """Regression: `seed()`s abschließendes `set_projekt_status()` schrieb bisher bei jedem Aufruf
    einen neuen status_log-Eintrag, auch wenn sich der Status gar nicht änderte - ein
    wiederholter Reset (`scripts/seed_demo_data.py`) häufte so beliebig viele "Neu -> Neu"-
    Einträge an. Zählt über alle Projekte (nicht `list_projekte(conn)[0]`, dessen Reihenfolge bei
    knapp beieinanderliegenden `aktualisiert_am`-Zeitstempeln aus demselben `seed()`-Lauf nicht
    verlässlich ist)."""
    demo = demo_data.demo_settings(settings)
    with closing(database.connect(demo)) as conn:
        demo_data.seed(conn)
        anzahl_erst = conn.execute("SELECT COUNT(*) FROM status_log").fetchone()[0]

        demo_data.seed(conn)
        demo_data.seed(conn)
        anzahl_danach = conn.execute("SELECT COUNT(*) FROM status_log").fetchone()[0]

    assert anzahl_erst == anzahl_danach


def test_demo_tier_by_quelle_covers_all_seeded_quellen(settings):
    demo = demo_data.demo_settings(settings)
    with closing(database.connect(demo)) as conn:
        demo_data.seed(conn)
        quellen_ids = {row["id"] for row in database.list_quellen(conn)}

    assert quellen_ids <= set(demo_data.DEMO_TIER_BY_QUELLE)
