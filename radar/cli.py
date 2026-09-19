"""CLI-Einstiegspunkt (Schritt 7): `python -m radar <befehl>`.

Befehle:
  scrape      - einen vollständigen Lauf ausführen und beenden (radar.scraper.run_full()).
                --notify sendet danach eine E-Mail-Benachrichtigung (radar/notifier.py), falls
                SMTP konfiguriert ist und neue Treffer gefunden wurden - aus, damit ein normaler
                Testlauf niemanden anmailt; für gezielte Demo-/Produktivläufe explizit setzen.
  scheduler   - Dauerprozess, führt run_full() nach Zeitplan aus (config/settings.yaml:
                scheduler) und benachrichtigt danach immer (kein --notify nötig/vorhanden)
  test-email  - SMTP-Konfiguration mit Beispieldaten testen, unabhängig von einem echten Lauf
                (radar/notifier.py)

Das Dashboard (`streamlit run app.py`) hat einen eigenen Einstiegspunkt: Streamlit-Apps starten
nicht über eine normale Python-CLI, siehe docker-compose.yml.
"""

from __future__ import annotations

import argparse
import logging
import sys

from radar.notifier import NeuerTreffer, load_smtp_config, send_notification
from radar.scheduler import run_forever
from radar.scraper import run_full

_BEISPIEL_TREFFER = [
    NeuerTreffer(
        kommune="Norderstedt",
        titel="Wohnpark Ochsenzoll (Beispiel)",
        we_gesamt=18,
        url="https://buergerinfo.norderstedt.de/ratsinfo/sessionnet/buergerinfo/",
    ),
    NeuerTreffer(
        kommune="Hamburg-Bergedorf",
        titel="Quartier Am Schleusengraben (Beispiel)",
        we_gesamt=42,
        url="https://bv-hh.de/",
    ),
]


def _test_email() -> int:
    """`python -m radar test-email`: E-Mail-Versand unabhängig von einem echten Scraper-Lauf
    prüfen. Meldet fehlende SMTP-Konfiguration explizit statt nur still zu loggen (anders als im
    Scheduler-Lauf, wo eine nicht konfigurierte Benachrichtigung bewusst kein Fehler ist)."""
    config = load_smtp_config()
    if config is None:
        print(
            "SMTP ist nicht konfiguriert: SMTP_SERVER/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/"
            "NOTIFICATION_RECIPIENT in .env setzen (siehe .env.example)."
        )
        return 1

    if send_notification(_BEISPIEL_TREFFER, config=config):
        empfaenger = ", ".join(config.recipients)
        print(f"Test-E-Mail mit {len(_BEISPIEL_TREFFER)} Beispiel-Treffern an {empfaenger} gesendet.")
        return 0
    print("Versand fehlgeschlagen (siehe Log für Details).")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description="Bauträger-Radar CLI")
    subparsers = parser.add_subparsers(dest="befehl", required=True)
    scrape_parser = subparsers.add_parser("scrape", help="Einen vollständigen Lauf ausführen und beenden")
    scrape_parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "E-Mail-Benachrichtigung senden, falls SMTP konfiguriert ist und neue Treffer "
            "gefunden wurden (radar/notifier.py). Standardmäßig aus, damit manuelle Testläufe "
            "nicht den Vertrieb spammen - für gezielte Demo-/Produktivläufe explizit setzen."
        ),
    )
    subparsers.add_parser("scheduler", help="Dauerprozess: Lauf nach Zeitplan (config/settings.yaml)")
    subparsers.add_parser("test-email", help="SMTP-Konfiguration mit Beispieldaten testen (radar/notifier.py)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.befehl == "scrape":
        results = run_full()
        if args.notify:
            neue_treffer = [treffer for r in results for treffer in r.neue_treffer]
            send_notification(neue_treffer)  # no-op, falls leer oder SMTP nicht konfiguriert
        return 0 if all(r.ok for r in results) else 1

    if args.befehl == "test-email":
        return _test_email()

    run_forever()  # args.befehl == "scheduler" (argparse lässt keinen anderen Wert zu)
    return 0


if __name__ == "__main__":
    sys.exit(main())
