# Richtet in der Windows-Aufgabenplanung einen wöchentlichen Lauf des Bauträger-Radars ein
# (Montag 03:00, wie scheduler.weekday/hour in config/settings.yaml). Einmalig ausführen:
#
#   powershell -ExecutionPolicy Bypass -File "D:\Projekte\Claude\bautraeger-radar\scripts\register_weekly_task.ps1"
#
# Erneutes Ausführen ersetzt die bestehende Aufgabe (-Force), z. B. nach einer Änderung an diesem Skript.
#
# Keine Administratorrechte nötig: die Aufgabe läuft unter dem angemeldeten Benutzer.
# "StartWhenAvailable" holt einen verpassten Termin nach (Rechner war aus) - sonst entstünde wieder
# eine Lücke wie zwischen 19.09. und 11/2026.
# Entfernen:  Unregister-ScheduledTask -TaskName "Bautraeger-Radar woechentlich" -Confirm:$false
# Nur für den lokalen Betrieb - im Docker-Betrieb übernimmt der Dienst "scheduler" den Lauf.

param(
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$Wochentag = "Monday",
    [string]$Uhrzeit = "03:00"
)

$ErrorActionPreference = "Stop"
$skript = Join-Path $PSScriptRoot "run_weekly.ps1"
$name = "Bautraeger-Radar woechentlich"

# -WindowStyle Hidden: am 23.11.2026 holte Windows den verpassten Montagslauf tagsüber nach, das sichtbare schwarze
# Fenster wurde nach Sekunden geschlossen (0xC000013A) und der Lauf damit abgebrochen. Ohne Fenster passiert das nicht;
# Fortschritt und Ergebnis stehen ohnehin in data\logs\woechentlich_<Datum>.log und im Dashboard.
$aktion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$skript`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$ausloeser = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Wochentag -At $Uhrzeit
# Der erste Lauf mit Backfill (scan.backfill_ab) kann mehrere Stunden dauern -> großzügiges Zeitlimit.
$optionen = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $name -Action $aktion -Trigger $ausloeser -Settings $optionen `
    -Description "Bauträger-Radar: wöchentlicher Lauf (scripts/run_weekly.ps1)" -Force | Out-Null

Write-Host "Aufgabe '$name' eingerichtet: jeden $Wochentag um $Uhrzeit."
Write-Host "Sofort testen: Start-ScheduledTask -TaskName '$name'"
