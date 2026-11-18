# Wöchentlicher Lauf für den lokalen Betrieb unter Windows (ohne Docker).
# Wird von der Windows-Aufgabenplanung aufgerufen (Einrichtung: scripts/register_weekly_task.ps1),
# kann aber auch von Hand gestartet werden:  powershell -ExecutionPolicy Bypass -File scripts\run_weekly.ps1
#
# Macht dasselbe wie der Docker-Dienst "scheduler" (radar/scheduler.py): ein vollständiger Lauf mit
# E-Mail bei neuen Treffern (nur wenn SMTP in .env eingerichtet ist). Ausgabe landet in
# data/logs/woechentlich_<Datum>.log. Läuft schon ein Lauf (z. B. über den Dashboard-Knopf), bricht
# dieser hier sauber ab (Sperrdatei data/lauf.lock) - beim nächsten Termin geht es normal weiter.

$ErrorActionPreference = "Stop"
$projekt = Split-Path -Parent $PSScriptRoot
Set-Location $projekt

$python = Join-Path $projekt ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python-Umgebung fehlt: $python (siehe README, Abschnitt Einrichtung)"
}

$logDir = Join-Path $projekt "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("woechentlich_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmm"))

# Python schreibt sein Log auf stderr; cmd /c leitet beides in die Datei um, ohne dass PowerShell
# jede Zeile als Fehler einstuft.
cmd /c "`"$python`" -m radar scrape --notify > `"$log`" 2>&1"
exit $LASTEXITCODE
