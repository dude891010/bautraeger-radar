# Sicherheitsprüfung Bauträger-Radar

Stand: 03.10.2026. Geprüft: Streamlit-Dashboard (`app.py`), Scraper-/HTTP-Schicht
(`radar/http_client.py`, `radar/scraper.py`), LLM-Anbindung (`radar/parser.py`), Konfiguration
und Betrieb (Docker, ngrok-Tunnel). Methode: Code-Review aus Angreifersicht plus automatisierte
Tests für jeden Fix (`tests/test_sicherheit.py`, `tests/test_kostenschutz.py`).

## Befunde und Fixes

| # | Schwere | Schwachstelle | Fix |
|---|---|---|---|
| 1 | **kritisch** | Dashboard ohne jede Anmeldung. Über einen ngrok-Tunnel (öffentliche URL) konnte jeder Projektdaten lesen/exportieren, Status/Notizen ändern, das Lauf-Log lesen und kostenpflichtige Läufe starten. | Eigener Passwortdialog (`radar/auth.py`): `DASHBOARD_PASSWORD` Pflicht (≥ 12 Zeichen), ohne Eintrag startet das Dashboard nicht. Vergleich in konstanter Zeit, Sperre nach 10 Fehlversuchen in 15 Min. (prozessweit), Abmelde-Knopf. |
| 2 | **hoch** | ngrok-Tunnel ohne Zugangsschutz (`ngrok http 8501`). | `scripts/start_tunnel.ps1`: startet ngrok nur mit Basic Auth (Traffic Policy) **und** gesetztem Dashboard-Passwort; Policy-Datei mit Passwort nur temporär. Zwei unabhängige Schutzschichten. |
| 3 | **hoch** | Kosten durch wiederholtes „Lauf jetzt starten“: jeder Lauf bekam ein volles Budget (300 Anfragen), beliebig oft hintereinander. | `llm.max_calls_per_day` (600, harte Obergrenze 3000) über **alle** Läufe eines Tages, jede Anfrage sofort in der Datenbank verbucht (`llm_nutzung`). Anzeige im Dashboard. |
| 4 | mittel | SSRF über HTTP-Weiterleitungen: `requests` folgte Weiterleitungen ungeprüft. Eine manipulierte Quelle hätte den Scraper im Firmennetz auf interne Adressen (Intranet, Cloud-Metadaten) umlenken können. | Weiterleitungen werden selbst verfolgt, jedes Ziel gegen Host-Freigabe, Schema (`http`/`https`) und robots.txt geprüft, max. 5 Sprünge. |
| 5 | mittel | Prompt-Injection: ein Dokument mit dem Text `</dokument>` konnte die Datengrenze im Prompt verlassen. | Steuer-Tags (`<dokument>`, `<fehler>`) im Dokumenttext werden entschärft. Die Antwort ist ohnehin auf das Pydantic-Schema beschränkt, Kosten sind durchs Budget gedeckelt. |
| 6 | mittel | Dashboard-Bearbeitungen kommen ungeprüft aus dem Browser (gesperrte Spalten wirken nur in der Oberfläche): fremde Projekt-IDs, beliebige Statuswerte, Notizen beliebiger Länge. | `find_changed_rows`: ID immer aus dem Original, nur bekannte Zeilen, Status nur aus `dashboard.statuses`, Notizen max. 2000 Zeichen. |
| 7 | mittel | Docker veröffentlichte Port 8501 auf allen Netzwerkschnittstellen des Hosts. | `docker-compose.yml`: `127.0.0.1:8501:8501`, nur noch für den Reverse Proxy auf demselben Host erreichbar. |
| 8 | niedrig | Manipulierte PDFs mit zehntausenden Seiten binden CPU/Speicher. | `MAX_PDF_SEITEN = 1000` in `radar/parser.py`. |
| 9 | niedrig | Streamlit zeigte Tracebacks im Browser, Entwickler-Menü sichtbar, Telemetrie an. | `.streamlit/config.toml`: `showErrorDetails = "type"`, `toolbarMode = "viewer"`, `gatherUsageStats = false`, XSRF-Schutz festgeschrieben. |

## API-Keys: Bewertung

Gut gelöst und unverändert: Der Anthropic-Key steht nur in `.env`. Die Datei ist per
`.gitignore` ausgeschlossen und war laut Git-Historie nie eingecheckt. Per `.dockerignore` ist
sie auch nicht im Image. Der Key wird zur Laufzeit gelesen, nicht geloggt und nie im Dashboard
angezeigt.

**Empfehlung (außerhalb des Codes, wichtigste harte Grenze):** In der Anthropic Console einen
eigenen Workspace für das Radar mit **monatlichem Ausgabenlimit** anlegen und nur dessen Key in
`.env` eintragen. Das begrenzt den Schaden auch dann, wenn der Key abfließt oder ein künftiger
Fehler alle Code-Limits umgeht. Bei Verdacht auf Abfluss den Key in der Console sofort
widerrufen.

## ngrok: Betriebsregeln

- Nur über `.\scripts\start_tunnel.ps1` starten, nie direkt mit `ngrok http 8501`.
- Den Tunnel nur so lange offen lassen wie nötig (z. B. für eine Präsentation), danach mit
  Strg+C beenden.
- Die URL nicht in E-Mails/Chats mit Externen teilen, Zugangsdaten getrennt übermitteln.
- Für Dauerbetrieb keinen ngrok-Tunnel nutzen, sondern den Docker-Betrieb hinter dem Reverse
  Proxy/SSO der Firmen-IT (dann optional `DASHBOARD_AUTH=extern`).
- Besser als Basic Auth wäre ngroks OAuth-Anmeldung (z. B. Google/Microsoft, beschränkt auf
  die Firmendomain). Das erfordert ein passendes ngrok-Konto und wurde hier nicht eingerichtet.

## Bewusst offen gebliebene Restrisiken

- HTML-/JSON-Antworten der Quellen haben keine eigene Größenbegrenzung (nur PDFs: 25 MB).
  Betroffen sind nur fest freigegebene Hosts.
- Der Abruf der `robots.txt` folgt Weiterleitungen weiterhin automatisch: Er wird nur als
  robots.txt ausgewertet und nie weitergegeben. Eine strengere Prüfung würde legitime
  Weiterleitungen (z. B. auf `www.`) brechen und damit die robots.txt-Einhaltung schwächen.
- Die Sperre nach Fehlversuchen gilt für alle gleichzeitig. Ein Angreifer könnte damit
  berechtigte Nutzer 15 Minuten aussperren. Das wird bewusst in Kauf genommen, statt
  Durchprobieren zuzulassen.
- Das Basic-Auth-Format der ngrok-Traffic-Policy ist nach ngrok-Dokumentation umgesetzt, aber
  nicht live getestet (das hätte einen öffentlichen Tunnel geöffnet). Nach dem ersten Start
  einmal prüfen: Die URL muss im Browser zuerst nach Benutzername/Passwort fragen.
