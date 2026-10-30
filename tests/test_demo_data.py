"""Tests für radar/demo_data.py: Demo-Seed-Datenbank für den Dashboard-"Demo-Daten anzeigen"-
Schalter. Reines SQLite, kein Netzzugriff, `tmp_path` pro Test (wie tests/test_database.py)."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pandas as pd
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


# -- Echte Frühphasen-Projekte (Richtweg, SonnenHeyde) im Tab "Projekte" ------------------------------


def test_echte_fruehphasen_projekte_stehen_sichtbar_in_der_demo(settings):
    demo = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo)) as conn:
        echte = {p["projektbezeichnung"]: p for p in database.list_projekte(conn)
                 if p["quelle_id"] == demo_data.ECHTE_QUELLE_ID}
    assert set(echte) == {"Richtweg / Garstedter Dreieck (Instone)", "SonnenHeyde (Manke)"}
    assert (echte["Richtweg / Garstedter Dreieck (Instone)"]["verfahrensstand"], echte[
        "Richtweg / Garstedter Dreieck (Instone)"]["we_gesamt"]) == ("AUFSTELLUNGSBESCHLUSS", 316)
    assert (echte["SonnenHeyde (Manke)"]["verfahrensstand"], echte["SonnenHeyde (Manke)"]["we_gesamt"]) == (
        "VORBERATUNG", 144)


def test_echte_projekte_idempotent_und_reset_setzt_status_zurueck(settings):
    demo = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo)) as conn:
        demo_data.seed_echte_projekte(conn)  # zweites Mal: keine Duplikate
        echte = [p for p in database.list_projekte(conn) if p["quelle_id"] == demo_data.ECHTE_QUELLE_ID]
        assert len(echte) == 2
        database.set_projekt_status(conn, echte[0]["id"], "Kontaktversuch")
    demo_data.reset_demo(settings)
    with closing(database.connect(demo)) as conn:
        assert all(p["status"] == "Neu" for p in database.list_projekte(conn)
                   if p["quelle_id"] == demo_data.ECHTE_QUELLE_ID)


def test_echte_projekte_sind_kein_kurzfristiger_lead_und_haben_klartext_stand(settings):
    from app import HERKUNFT_DEMO, HERKUNFT_ECHT, add_herkunft_und_stand, add_lead_badge, build_projekte_df

    demo = demo_data.ensure_demo_seeded(settings)
    with closing(database.connect(demo)) as conn:
        df = build_projekte_df(database.list_projekte(conn), {})
    df = add_herkunft_und_stand(add_lead_badge(df), demo_modus=True).set_index("projektbezeichnung")
    assert df.loc["Richtweg / Garstedter Dreieck (Instone)", "stand"] == "B-Plan / In Planung (Aufstellung)"
    assert df.loc["SonnenHeyde (Manke)", "stand"] == "Geplant / Vorbereitung"
    assert df.loc["SonnenHeyde (Manke)", "herkunft"] == HERKUNFT_ECHT
    assert df.loc["SonnenHeyde (Manke)", "lead"] == ""  # Frühphase ist kein kurzfristiger Lead
    assert df.loc["Richtweg / Garstedter Dreieck (Instone)", "lead"] == ""
    andere = df[df["quelle_id"] != demo_data.ECHTE_QUELLE_ID]
    assert set(andere["herkunft"]) == {HERKUNFT_DEMO}


# -- 5 geprüfte Norderstedter Projekte (scripts/seed_demo_data.py --norderstedt) -----------------------


def _norderstedt_db(settings):
    with closing(database.connect(settings)) as conn:
        demo_data.seed_echte_projekte(conn)
        demo_data.seed_norderstedt_projekte(conn)
        demo_data.seed_norderstedt_projekte(conn)  # zweimal: idempotent
        return {p["projektbezeichnung"]: dict(p) for p in database.list_projekte(conn)}


def test_norderstedt_projekte_idempotent_mit_quelle_und_notiz(settings):
    projekte = _norderstedt_db(settings)
    assert len(projekte) == 7  # 5 neue + Richtweg + SonnenHeyde
    garstedt = projekte["Berliner Allee – Quartier Garstedt"]
    assert (garstedt["we_gesamt"], garstedt["we_gefoerdert_anzahl"], garstedt["verfahrensstand"]) == (
        198, 60, "BAUGENEHMIGUNG")
    assert garstedt["quelle_url"].startswith("https://www.konii.de/news/quarterback")
    assert garstedt["notizen"].startswith("Kurz vor Fertigstellung")
    assert projekte["B-Plan 343 „Meyers Mühle“"]["verfahrensstand"] == "AUSLEGUNG"
    assert projekte["B-Plan 339 „zwischen Berliner Allee, Schumanstraße und Adenauerplatz“"][
        "verfahrensstand"] == "AUFSTELLUNGSBESCHLUSS"


def test_glashuetter_kirchenweg_hat_keine_erfundene_we_zahl(settings):
    from app import add_herkunft_und_stand, add_pruef_badge, build_projekte_df

    with closing(database.connect(settings)) as conn:
        demo_data.seed_norderstedt_projekte(conn)
        df = build_projekte_df(database.list_projekte(conn), {})
    df = add_herkunft_und_stand(add_pruef_badge(df), demo_modus=False).set_index("projektbezeichnung")
    zeile = df.loc["Rahmenplan „Glashütter Kirchenweg“"]
    assert pd.isna(zeile["we_gesamt"]) and zeile["we_anzeige"] == "WE offen"
    assert zeile["stand"] == "Geplant / Vorbereitung"
    assert "Keine Zahl der Wohneinheiten" in zeile["pruefauftrag"]
    assert df.loc["B-Plan 319 „Quartier südlich Moorbektwiete / westlich Ulzburger Straße“", "we_anzeige"] == "26"


def test_manuelle_notiz_bleibt_beim_erneuten_seeden(settings):
    with closing(database.connect(settings)) as conn:
        demo_data.seed_norderstedt_projekte(conn)
        projekte = database.list_projekte(conn)
        pid = next(p["id"] for p in projekte if p["projektbezeichnung"] == "B-Plan 343 „Meyers Mühle“")
        database.set_projekt_notizen(conn, pid, "Investor angerufen")
        demo_data.seed_norderstedt_projekte(conn)
        assert next(p for p in database.list_projekte(conn) if p["id"] == pid)["notizen"] == "Investor angerufen"


def test_seed_skript_verweigert_echte_datenbank_und_schreibt_test_db(settings, monkeypatch, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("seed_demo_data", Path(__file__).parent.parent / "scripts" /
                                                  "seed_demo_data.py")
    skript = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skript)
    monkeypatch.setattr(skript, "load_settings", lambda: settings)
    assert skript.main(["--norderstedt", "--db", settings.database.path]) == 2
    test_db = tmp_path / "test_radar.db"
    assert skript.main(["--norderstedt", "--db", str(test_db)]) == 0
    with closing(database.connect(replace(settings, database=DatabaseConfig(path=str(test_db))))) as conn:
        assert len(database.list_projekte(conn)) == 7


def test_bplaene_ohne_belegte_wohnform_bekommen_pruefauftrag(settings):
    from app import add_pruef_badge, build_projekte_df

    with closing(database.connect(settings)) as conn:
        demo_data.seed_norderstedt_projekte(conn)
        df = add_pruef_badge(build_projekte_df(database.list_projekte(conn), {})).set_index("projektbezeichnung")
    bplaene = [n for n in df.index if n.startswith("B-Plan")]
    assert len(bplaene) == 3
    for name in bplaene:
        assert df.loc[name, "wohnform"] == "UNKLAR"
        assert "Geförderter Anteil bekannt" in df.loc[name, "pruefauftrag"]
    assert df.loc["Berliner Allee – Quartier Garstedt", "pruefen"] == ""
