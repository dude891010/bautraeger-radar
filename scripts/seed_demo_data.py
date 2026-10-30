"""Demo-/Beispieldatenbank für das Dashboard anlegen oder zurücksetzen (siehe radar/demo_data.py).

Das Dashboard befüllt data/demo/demo.db automatisch selbst, sobald der Schalter "Demo-Daten
anzeigen" zum ersten Mal aktiviert wird (leere Demo-DB) - dieses Skript ist für den manuellen
Reset auf die ursprünglichen Beispielwerte, z. B. nachdem im Demo-Modus im Dashboard-Editor
Status/Notizen verändert wurden. Kein Netzzugriff, keine Anthropic-API, berührt nie data/radar.db.

    .venv\\Scripts\\python.exe scripts\\seed_demo_data.py

Mit --norderstedt werden stattdessen die geprüften echten Norderstedter Projekte (Richtweg, SonnenHeyde
und die 5 aus der Recherche vom 30.10.2026) in eine Demo-/Test-Datenbank geschrieben, idempotent:

    .venv\\Scripts\\python.exe scripts\\seed_demo_data.py --norderstedt --db data\\test_radar.db
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import database, demo_data  # noqa: E402
from radar.config import DatabaseConfig, load_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--norderstedt", action="store_true", help="Geprüfte echte Norderstedter Projekte schreiben")
    parser.add_argument("--db", metavar="PFAD", help="Ziel-Datenbank für --norderstedt, z. B. data/test_radar.db")
    args = parser.parse_args(argv)
    settings = load_settings()

    if not args.norderstedt:
        demo_settings = demo_data.demo_settings(settings)
        with closing(database.connect(demo_settings)) as conn:
            demo_data.seed(conn)
        print(f"Demo-Datenbank aktualisiert: {demo_data.demo_db_path(settings)}")
        return 0

    ziel = Path(args.db).resolve() if args.db else demo_data.demo_db_path(settings).resolve()
    if ziel == settings.resolve_path(settings.database.path).resolve():
        print(f"Abgelehnt: {ziel} ist die echte Datenbank - Demo-Daten nur in Demo-/Test-Datenbanken.")
        return 2
    with closing(database.connect(replace(settings, database=DatabaseConfig(path=str(ziel))))) as conn:
        demo_data.seed_echte_projekte(conn)
        demo_data.seed_norderstedt_projekte(conn)
    print(f"Echte Norderstedter Projekte geschrieben: {ziel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
