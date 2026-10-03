# Startet einen ngrok-Tunnel zum lokalen Dashboard (Port 8501) - aber NUR mit Zugangsschutz.
#
# Warum dieses Skript statt "ngrok http 8501": ein ngrok-Tunnel ist eine öffentliche
# Internetadresse. Ohne Schutz kann jeder, der sie kennt oder errät, Projektdaten lesen/ändern
# und kostenpflichtige Läufe starten. Dieses Skript erzwingt zwei Schutzschichten:
#   1. ngrok selbst verlangt Benutzername/Passwort (Basic Auth über eine Traffic Policy), bevor
#      überhaupt eine Anfrage beim Dashboard ankommt.
#   2. Das Dashboard verlangt zusätzlich sein eigenes Passwort (DASHBOARD_PASSWORD).
#
# Voraussetzungen in .env:
#   DASHBOARD_PASSWORD=...           (mind. 12 Zeichen)
#   NGROK_BASIC_AUTH_USER=...        (nur Buchstaben, Ziffern, . _ -)
#   NGROK_BASIC_AUTH_PASSWORD=...    (12-128 Zeichen, ohne " und \)
#
# Aufruf (PowerShell, im Projektordner):  .\scripts\start_tunnel.ps1
# Beenden: Strg+C - der Tunnel ist danach sofort weg. Tunnel nur so lange offen lassen wie nötig.

$ErrorActionPreference = "Stop"
$projekt = Split-Path $PSScriptRoot -Parent
$envDatei = Join-Path $projekt ".env"

if (-not (Test-Path $envDatei)) {
    Write-Error ".env nicht gefunden ($envDatei). Bitte aus .env.example anlegen."
    exit 1
}

$werte = @{}
foreach ($zeile in Get-Content $envDatei -Encoding UTF8) {
    if ($zeile -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') { $werte[$Matches[1]] = $Matches[2] }
}

$dashboardPasswort = $werte["DASHBOARD_PASSWORD"]
$benutzer = $werte["NGROK_BASIC_AUTH_USER"]
$passwort = $werte["NGROK_BASIC_AUTH_PASSWORD"]

if (-not $dashboardPasswort -or $dashboardPasswort.Length -lt 12) {
    Write-Error "DASHBOARD_PASSWORD fehlt oder ist kürzer als 12 Zeichen - Tunnel wird NICHT gestartet."
    exit 1
}
if (-not $benutzer -or $benutzer -notmatch '^[A-Za-z0-9._-]+$') {
    Write-Error "NGROK_BASIC_AUTH_USER fehlt oder enthält unerlaubte Zeichen - Tunnel wird NICHT gestartet."
    exit 1
}
if (-not $passwort -or $passwort.Length -lt 12 -or $passwort.Length -gt 128 -or $passwort -match '["\\]') {
    Write-Error "NGROK_BASIC_AUTH_PASSWORD fehlt, ist kürzer als 12/länger als 128 Zeichen oder enthält `" bzw. \ - Tunnel wird NICHT gestartet."
    exit 1
}
if ($passwort -eq $dashboardPasswort) {
    Write-Error "NGROK_BASIC_AUTH_PASSWORD und DASHBOARD_PASSWORD müssen verschieden sein - Tunnel wird NICHT gestartet."
    exit 1
}

# Traffic Policy nur temporär auf der Platte (enthält das Passwort) und nach dem Beenden gelöscht.
$policy = @"
on_http_request:
  - actions:
      - type: basic-auth
        config:
          realm: "Bautraeger-Radar"
          credentials:
            - "$($benutzer):$($passwort)"
"@
$policyDatei = Join-Path $env:TEMP ("radar-ngrok-" + [guid]::NewGuid().ToString() + ".yml")
[System.IO.File]::WriteAllText($policyDatei, $policy, (New-Object System.Text.UTF8Encoding $false))

try {
    Write-Host "Starte ngrok-Tunnel mit Basic Auth (Benutzer '$benutzer'). Beenden mit Strg+C."
    & ngrok http 8501 --traffic-policy-file $policyDatei
}
finally {
    Remove-Item $policyDatei -Force -ErrorAction SilentlyContinue
}
