"""Geplanter Lauf (Schritt 7): `radar.scraper.run_full()` wöchentlich ausführen.

Läuft als eigener Dauerprozess (siehe docker-compose.yml, Service "scheduler") statt über
System-Cron: kein zusätzliches Paket, keine Cron-Rechte für den Nicht-Root-Nutzer im Container
nötig, und dieselbe Logik lässt sich ohne Netzzugriff testen (siehe tests/test_scheduler.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from radar.config import Settings, load_settings
from radar.notifier import send_notification
from radar.scraper import run_full

logger = logging.getLogger(__name__)


def _next_run(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    """Nächster Zeitpunkt mit gegebenem Wochentag (0=Montag) und Uhrzeit, garantiert in der
    Zukunft (auch wenn `now` selbst genau auf den Zeitpunkt fällt -> eine Woche weiter)."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - now.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def run_forever(
    settings: Settings | None = None,
    *,
    max_runs: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = datetime.now,
) -> None:
    """Wartet bis zum nächsten geplanten Zeitpunkt (config/settings.yaml: `scheduler`) und führt
    dann `run_full()` aus, danach von vorn. Ein Fehlschlag eines Laufs beendet den Prozess nicht
    (derselbe Grundsatz wie bei einzelnen Quellen: nie den ganzen Betrieb stoppen).

    `max_runs`/`sleep`/`now` sind ausschließlich für Tests da (sonst Endlosschleife mit echtem
    `time.sleep`); im Produktivbetrieb bleiben sie auf ihren Standardwerten."""
    settings = settings or load_settings()
    cfg = settings.scheduler
    runs = 0
    while max_runs is None or runs < max_runs:
        aktuell = now()
        ziel = _next_run(aktuell, cfg.weekday, cfg.hour, cfg.minute)
        logger.info("Nächster geplanter Lauf: %s", ziel.isoformat())
        while True:
            rest = (ziel - now()).total_seconds()
            if rest <= 0:
                break
            sleep(min(cfg.check_interval_seconds, rest))

        logger.info("Geplanter Lauf startet")
        try:
            results = run_full(settings)
            neue_treffer = [treffer for r in results for treffer in r.neue_treffer]
            send_notification(neue_treffer)  # no-op, falls leer oder SMTP nicht konfiguriert
        except Exception:
            logger.exception("Geplanter Lauf fehlgeschlagen")
        runs += 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_forever()
