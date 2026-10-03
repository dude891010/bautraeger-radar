# Bauträger-Radar

Erkennt Wohnbauprojekte (Miet- und geförderter Wohnungsbau) in den öffentlichen Sitzungsunterlagen
von Kommunen in Schleswig-Holstein, Hamburg, Niedersachsen, Bremen, Mecklenburg-Vorpommern und
selektiv Berlin. MVP-Referenzquelle: Norderstedt (Ausschuss für Stadtentwicklung und Verkehr).
Vollständige Spezifikation und Regeln: [CLAUDE.md](CLAUDE.md). Weitere Quellen: [docs/QUELLENKATALOG.md](docs/QUELLENKATALOG.md).

## Einrichtung

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env               # dann SCRAPER_CONTACT (und ab Schritt 3 ANTHROPIC_API_KEY) eintragen
pytest
streamlit run app.py               # Dashboard starten
```

## Betrieb per Docker

```bash
cp .env.example .env               # SCRAPER_CONTACT und ANTHROPIC_API_KEY eintragen
docker compose up -d --build
```

Startet zwei Dienste aus demselben Image (siehe `Dockerfile`/`docker-compose.yml`):
`dashboard` (Streamlit unter http://localhost:8501 – ohne eigene Authentifizierung, im Betrieb
nur hinter Reverse Proxy/SSO/Basic Auth der Firmen-IT exponieren) und `scheduler`
(`radar/scheduler.py`, führt den Lauf wöchentlich aus, Zeitpunkt in `config/settings.yaml`).
Beide teilen sich `./data` als Volume (Datenbank, PDF-/LLM-Cache, Logs). `scripts/run_daily.sh`
löst einen Lauf sofort aus, ohne auf den nächsten planmäßigen Zeitpunkt zu warten.

Optional: E-Mail-Benachrichtigung bei neuen Treffern (`radar/notifier.py`) – `SMTP_SERVER`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `NOTIFICATION_RECIPIENT` (eine oder mehrere,
kommagetrennte Adressen) in `.env` eintragen, sonst bleibt sie deaktiviert. Unabhängig testen:
`python -m radar test-email`. Der `scheduler`-Dienst benachrichtigt immer; ein manueller Lauf nur
mit `python -m radar scrape --notify` bzw. der Dashboard-Checkbox "E-Mail-Benachrichtigung
senden" (Default: aus, damit normale Testläufe nicht den Vertrieb anmailen).

## Struktur

| Pfad | Zweck |
|---|---|
| `config/settings.yaml` | Globale Einstellungen: Regionen/Tiers, Filter, Scraper, LLM |
| `config/sources.yaml` | Quellenregister: eine Zeile pro Ratsinformationssystem |
| `radar/sources/` | Adapter je Systemtyp (SessionNet, ALLRIS, OParl, Transparenzportal Hamburg, bv-hh.de) |
| `radar/` | `config`, `committees`, `scraper`, `parser`, `database`, `notifier` (E-Mail-Benachrichtigung) |
| `app.py` | Streamlit-Dashboard (`streamlit run app.py`) |
| `tests/` | Tests, `tests/fixtures/` für gespeicherte HTML-Antworten, `tests/gold/` für den LLM-Goldstandard |
| `config/prompts/` | Triage-/Extraktions-Prompts (versioniert, nicht im Python-Code) |
| `scripts/run_goldstandard.py` | Manuelle Goldstandard-Auswertung (echte, kostenpflichtige LLM-Aufrufe) |
| `scripts/run_daily.sh` | Lauf sofort auslösen (Docker), statt auf den Scheduler zu warten |
| `data/` | SQLite-Datenbank, PDF-Cache und LLM-Ergebnis-Cache (nicht im Git) |
| `docs/` | Quellenkatalog, Onboarding neuer Quellen, IT-Übergabedokumentation |
| `Dockerfile` / `docker-compose.yml` | Container-Image, Dienste `dashboard` (Streamlit) und `scheduler` |

## Stand
Schritt 1, 2, 4 und 5 abgeschlossen, Schritt 3 (Parser) implementiert und offline getestet, Schritt 6
(weitere Adapter) in Arbeit: gemeinsamer HTTP-Client (`radar/http_client.py`, robots.txt-Prüfung über
`protego`, mehrere Hosts je Quelle möglich), vier Adapter neben SessionNet – OParl (Referenz: Gemeinde
Uplengen), das offizielle Transparenzportal Hamburg (Bebauungspläne, CKAN-API) und bv-hh.de (inoffizielle
Ergänzung für die frühen Verfahrensstufen der 7 Hamburger Bezirksversammlungen, liefert Text statt PDF
über `Dokument.text`/`Textinhalt`) – alle vier verifiziert gegen echte Live-Antworten. PDF-Text/
Keyword-Vorfilter/LLM-Triage- und -Extraktion (`radar/parser.py`, Pydantic-validiert, Budget + Cache), die
SQLite-Persistenz mit zweistufiger Dedup und Statuspflege (`radar/database.py`) und ein
Streamlit-Dashboard (`app.py`: Projekte filtern/bearbeiten, CSV-/Excel-Export, Quellen-Monitoring, Lauf
per Knopfdruck im Hintergrund). `radar/scraper.py` verdrahtet alles zu einem vollständigen Lauf
(`run_full()`) – `python -m radar.scraper` führt ihn direkt aus, `streamlit run app.py` startet ihn per
Knopf. Die einzige aktivierte Produktivquelle ist weiterhin Norderstedt; alle vier neu verifizierten
Quellen (Uplengen, Transparenzportal Hamburg, 7× bv-hh.de) sind bewusst deaktiviert (`enabled: false`) –
Details und Gründe (robots.txt-Auslegung, fehlende PDF-Anlagen, Verlässlichkeit) unter CLAUDE.md,
Abschnitt "Referenzquelle Uplengen/OParl" bzw. "Referenzquelle Hamburg". Ein echter Live-Aufruf der
Anthropic-API scheitert aktuell an fehlendem Guthaben (`anthropic.BadRequestError`), die
Goldstandard-Messung (`tests/gold/faelle.yaml`) und ein Live-Test des vollständigen Laufs stehen deshalb
noch aus. Schritt 7 (Docker/Scheduler/CLI) abgeschlossen: `python -m radar <scrape|scheduler>`
als CLI, `radar/scheduler.py` als Cron-freier Dauerprozess, `Dockerfile` + `docker-compose.yml`
(Dienste `dashboard`/`scheduler`, gemeinsames `data/`-Volume), `scripts/run_daily.sh` für
Ad-hoc-Läufe und [docs/IT_COMPLIANCE.md](docs/IT_COMPLIANCE.md) als Übergabedokumentation für
die Firmen-IT (Anbieter, Datenkategorien, Datenfluss, API-Key-Verwaltung; offene rechtliche
Punkte wie AVV/internationale Datenübermittlung bewusst an die Rechtsabteilung verwiesen, nicht
im Code entschieden). Nachträglich ergänzt: `radar/notifier.py` für eine optionale
E-Mail-Benachrichtigung bei neuen Treffern (SMTP über `.env`, mehrere kommagetrennte Empfänger
möglich, unabhängig testbar per `python -m radar test-email`), verdrahtet in
`radar/scheduler.py` (immer) und optional in manuelle Läufe (`python -m radar scrape --notify`,
Dashboard-Checkbox – Default aus). Details: [CLAUDE.md](CLAUDE.md), Abschnitt "Roadmap".

## Kostenschutz (Anthropic-API)
Ein Lauf kann nie unkontrolliert API-Kosten verursachen (`radar/parser.py`, `LLMBudget`/`_call`):
- **Jede HTTP-Anfrage zählt** gegen `llm.max_calls_per_run` (Standard 300, harte Obergrenze 1000 im
  Code) und `llm.max_calls_per_source` – auch Retries und Reparaturversuche. Die SDK-eigenen Retries
  sind abgeschaltet, damit nichts am Budget vorbei gesendet wird.
- **Begrenzte Retries** nur bei vorübergehenden Fehlern (429/5xx/Timeout), höchstens
  `llm.max_attempts_per_call` Versuche mit Wartezeit (`Retry-After` wird respektiert, max. 60 s).
- **Notbremse:** Guthaben leer, ungültiger Key, fehlende Berechtigung oder falscher Modellname
  stoppen sofort alle weiteren Anfragen des Laufs; ebenso `llm.max_consecutive_errors` Fehler in
  Folge. Das erscheint als Fehler im Quellen-Monitoring und im Exit-Code, nicht nur im Log.
- **Keine Dauerschleife über Läufe hinweg:** eine Vorlage, die `llm.max_fehlversuche_pro_vorlage`
  Läufe lang mit Fehler scheitert, wird nicht mehr versucht (Budget-Abbrüche zählen nicht).
- **Nie zwei Läufe gleichzeitig:** Sperrdatei `data/lauf.lock` (Dashboard-Knopf + Scheduler
  hätten sonst je ein volles Budget gehabt).
- Am Ende jedes Laufs steht der Verbrauch im Log (Anfragen, Tokens, ggf. Notbremse).

## Sicherheit
Das Dashboard verlangt ein Passwort (`DASHBOARD_PASSWORD` in `.env`, mindestens 12 Zeichen).
Ohne diesen Eintrag startet es nicht. Ein öffentlicher ngrok-Tunnel nur über
`.\scripts\start_tunnel.ps1` (erzwingt zusätzlich Basic Auth bei ngrok). Befunde, Fixes und
Betriebsregeln: [docs/SICHERHEIT.md](docs/SICHERHEIT.md).
