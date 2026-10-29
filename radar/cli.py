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
  app         - Dashboard starten (Streamlit), optional mit --db auf einer anderen Datenbank, z. B.
                `python -m radar app --db data/test_radar.db`. Läuft, bis es mit Strg+C beendet wird.
  web-recherche - Phase 3/4 (Baugenehmigung, Baubeginn) im Umkreis um Norderstedt per Such-API
                suchen (radar/web_recherche.py, braucht TAVILY_API_KEY in .env). --notify wie bei scrape.
                --demo (oder --test ohne Key): Beispieldaten statt echter Suche, 0 Credits.

Das Dashboard (`streamlit run app.py`) hat einen eigenen Einstiegspunkt: Streamlit-Apps starten
nicht über eine normale Python-CLI, siehe docker-compose.yml.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from radar.config import LIVE_AUS_HINWEIS
from radar.notifier import NeuerTreffer, load_smtp_config, send_notification
from radar.scheduler import run_forever
from radar.scraper import LaufLaeuftBereits, run_full
from radar.web_recherche import (
    GRUND_FRUEHPHASE,
    VERIFIZIERTE_DEMO_PROJEKTE,
    VERIFIZIERTE_DEMO_URLS,
    live_aufrufe_erlaubt,
    run_web_recherche,
)

WEB_TEST_ORTE = 3  # `web-recherche --test`: so viele nächstgelegene Orte, je 2 Anfragen

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


def _app_befehl(port: int) -> list[str]:
    """Ohne Datei-Überwachung (`fileWatcherType none`): die braucht nur, wer am Code arbeitet (Neuladen bei
    Änderungen), und sie hängt am Paket `watchdog` - ist das beschädigt, lädt das Dashboard gar nicht
    (so am 29.10.2026 lokal passiert: `No module named 'watchdog.observers.api'`)."""
    app_py = Path(__file__).resolve().parent.parent / "app.py"
    return [
        sys.executable, "-m", "streamlit", "run", str(app_py),
        "--server.port", str(port), "--server.fileWatcherType", "none",
    ]


def _app(db: str | None, port: int) -> int:
    """Dashboard als Kindprozess starten. Die Datenbank geht per $RADAR_DATABASE_PATH an
    radar.config.load_settings() - Streamlit-Skripte bekommen keine eigenen Kommandozeilen-Argumente."""
    env = dict(os.environ)
    if db:
        pfad = Path(db).resolve()
        if not pfad.exists():
            print(f"Datenbank {pfad} gibt es nicht (Tippfehler?). Für die Test-Datenbank zuerst: "
                  "python -m radar web-recherche --test --db data/test_radar.db")
            return 2
        env["RADAR_DATABASE_PATH"] = str(pfad)
        print(f"Dashboard auf {pfad}", flush=True)
    print(f"Dashboard: http://localhost:{port}  (beenden mit Strg+C)", flush=True)
    return subprocess.run(_app_befehl(port), env=env).returncode


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
    app_parser = subparsers.add_parser("app", help="Dashboard starten (Streamlit)")
    app_parser.add_argument("--db", metavar="PFAD", help="Andere Datenbank anzeigen, z. B. data/test_radar.db")
    app_parser.add_argument("--port", type=int, default=8501, help="Port (Standard 8501)")
    web_parser = subparsers.add_parser(
        "web-recherche", help="Baugenehmigungen/Baubeginne im Umkreis per Such-API suchen (radar/web_recherche.py)"
    )
    web_parser.add_argument(
        "--notify", action="store_true", help="Neue Web-Funde per E-Mail melden (wie bei scrape, Standard: aus)"
    )
    web_parser.add_argument(
        "--test",
        action="store_true",
        help=f"Testsuche: nur die {WEB_TEST_ORTE} nächstgelegenen Orte ({WEB_TEST_ORTE * 2} Anfragen statt aller)",
    )
    web_parser.add_argument(
        "--db", metavar="PFAD", help="Andere Datenbank verwenden, z. B. data/test_radar.db (Standard: settings.yaml)"
    )
    web_parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Beispieldaten statt echter Suche (kein Key, keine Credits) - für Live-Demos. Ohne --db in "
            "data/demo/demo.db (Dashboard: 'Demo-Daten anzeigen'), nie in die echte Datenbank."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.befehl == "scrape":
        try:
            results = run_full()
        except LaufLaeuftBereits as exc:
            logging.getLogger(__name__).error("Lauf nicht gestartet: %s", exc)
            return 2
        if args.notify:
            neue_treffer = [treffer for r in results for treffer in r.neue_treffer]
            send_notification(neue_treffer)  # no-op, falls leer oder SMTP nicht konfiguriert
        return 0 if all(r.ok for r in results) else 1

    if args.befehl == "test-email":
        return _test_email()

    if args.befehl == "app":
        return _app(args.db, args.port)

    if args.befehl == "web-recherche":
        demo = args.demo
        if not demo and not live_aufrufe_erlaubt():
            # Kein Guthaben/Plan: jeder Aufruf läuft als Demo (0 Credits, nie in die echte Datenbank).
            logging.getLogger(__name__).warning("%s - läuft als Demo", LIVE_AUS_HINWEIS)
            demo = True
        if args.test and not demo and not os.environ.get("TAVILY_API_KEY", "").strip():
            # Nur im Testmodus: statt abzubrechen auf Beispieldaten ausweichen. Ein normaler Lauf
            # (und der Scheduler) bekommt nie stillschweigend Demo-Daten untergeschoben.
            logging.getLogger(__name__).warning(
                "TAVILY_API_KEY fehlt - Testmodus weicht auf Demo-Daten aus (keine echte Suche)"
            )
            demo = True
        try:
            ergebnis = run_web_recherche(
                max_orte=WEB_TEST_ORTE if args.test else None, db_pfad=args.db, demo=demo
            )
        except (RuntimeError, ValueError) as exc:  # z. B. TAVILY_API_KEY fehlt, Demo auf echte DB
            logging.getLogger(__name__).error("Web-Recherche nicht gestartet: %s", exc)
            return 2
        if args.test or demo:
            if demo:
                print(
                    "\n*** DEMO-MODUS: Beispieldaten, keine echte Suche, 0 Credits. Erfunden (mit \"(Demo)\"), "
                    "außer den mit ⭐ markierten echten, geprüften Projekten ***"
                )
            print(f"\n{ergebnis.zusammenfassung()}")
            # Alle Funde des Laufs, nicht nur neue: sonst zeigt eine wiederholte Live-Demo fast nichts mehr.
            neue_urls = {f.url for f in ergebnis.neu_fuer_mail}
            for fund in ergebnis.alle_funde:
                we = fund.we_anzahl if fund.we_anzahl is not None else "?"
                neu = "NEU | " if fund.url in neue_urls else ""
                stern = "⭐ ECHTES PROJEKT (Quelle geprüft) | " if fund.url in VERIFIZIERTE_DEMO_URLS else ""
                print(f"- {neu}{stern}{fund.phase} | {fund.ort} ({fund.entfernung_km:.0f} km): {fund.titel} ({we} WE)")
                print(f"  {fund.url}")
            # Echte Projekte, die die Bewertung verworfen hat: zeigen, dass Frühphasen kein heißer Lead sind.
            for ort, titel, url, grund in ergebnis.verworfene_echte:
                art = "In Frühphase verworfen" if grund == GRUND_FRUEHPHASE else f"verworfen: {grund}"
                print(f"- ⭐ ECHTES PROJEKT ({art}) | {VERIFIZIERTE_DEMO_PROJEKTE[url]}, {ort}: {titel}")
                print(f"  Grund: {grund}")
                print(f"  {url}")
        if args.notify and demo:
            logging.getLogger(__name__).warning("--notify ignoriert: Demo-Daten werden nie per E-Mail verschickt")
        elif args.notify:
            send_notification([], web_funde=ergebnis.neu_fuer_mail)  # no-op, falls leer/nicht konfiguriert
        return 0 if ergebnis.ok else 1

    run_forever()  # args.befehl == "scheduler" (argparse lässt keinen anderen Wert zu)
    return 0


if __name__ == "__main__":
    sys.exit(main())
