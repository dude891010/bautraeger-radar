#!/usr/bin/env bash
# Manuellen Lauf sofort auslösen, statt auf den nächsten planmäßigen Zeitpunkt des
# "scheduler"-Diensts zu warten (config/settings.yaml: scheduler.weekday/hour/minute).
# Für Ad-hoc-Läufe (Test nach einer Konfigurationsänderung, Nachholen eines verpassten Laufs)
# oder als Alternative, falls die Firmen-IT lieber host-seitiges Cron statt des eingebauten
# Python-Schedulers (radar/scheduler.py) nutzen möchte - dann diesen Skript per Cron aufrufen,
# den "scheduler"-Dienst in docker-compose.yml in dem Fall nicht mitstarten.
#
# Voraussetzung: die Container laufen bereits (`docker compose up -d`), da sich Datenbank und
# Cache im gemeinsamen "data/"-Volume befinden.
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose run --rm scheduler python -m radar scrape
