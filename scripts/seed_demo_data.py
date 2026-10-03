"""Demo-/Beispieldatenbank für das Dashboard anlegen oder zurücksetzen (siehe radar/demo_data.py).

Das Dashboard befüllt data/demo/demo.db automatisch selbst, sobald der Schalter "Demo-Daten
anzeigen" zum ersten Mal aktiviert wird (leere Demo-DB) - dieses Skript ist für den manuellen
Reset auf die ursprünglichen Beispielwerte, z. B. nachdem im Demo-Modus im Dashboard-Editor
Status/Notizen verändert wurden. Kein Netzzugriff, keine Anthropic-API, berührt nie data/radar.db.

    .venv\\Scripts\\python.exe scripts\\seed_demo_data.py
"""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import database, demo_data  # noqa: E402
from radar.config import load_settings  # noqa: E402


def main() -> None:
    settings = load_settings()
    demo_settings = demo_data.demo_settings(settings)
    with closing(database.connect(demo_settings)) as conn:
        demo_data.seed(conn)
    print(f"Demo-Datenbank aktualisiert: {demo_data.demo_db_path(settings)}")


if __name__ == "__main__":
    main()
