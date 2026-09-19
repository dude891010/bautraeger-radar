# Übergabedokumentation für die IT-Abteilung

Dieses Dokument fasst zusammen, welcher externe Dienst eingebunden ist, welche Daten dorthin
gehen und wie der API-Zugang verwaltet wird (CLAUDE.md, Abschnitt "PDF- & LLM-Regeln":
"Unterlagen sind öffentlich, gehen aber an eine externe API. Für die Firmen-IT dokumentieren
(Anbieter, Datenkategorien, Speicherort)."). Es ersetzt keine rechtliche Prüfung durch die
Rechtsabteilung/den Datenschutzbeauftragten – die offenen Punkte dafür stehen am Ende.

## Anbieter & Subprozessor

**Anthropic, PBC** (Claude API, `anthropic.com`) wird für die strukturierte Extraktion von
Projektdaten aus Ratsunterlagen eingesetzt (`radar/parser.py`). Zwei Modelle im Einsatz
(`config/settings.yaml`: `llm.triage_model`/`llm.extraction_model`) – ein günstiges Modell für
eine grobe Ja/Nein-Vorprüfung, ein stärkeres Modell für die eigentliche Feldextraktion
(CLAUDE.md: "Dreistufig", Stufe 2/3). Beide laufen über dieselbe API und denselben
Vertragsrahmen.

Ein zweiter, unabhängig konfigurierter externer Datenfluss besteht optional für die
E-Mail-Benachrichtigung bei neuen Treffern (`radar/notifier.py`) – Details im eigenen Abschnitt
weiter unten, da hier kein Dokumenttext, sondern nur eine kurze Zusammenfassung übertragen wird.

## Verarbeitete Datenkategorien

Ausschließlich öffentlich zugängliche Kommunaldokumente: Ausschussvorlagen, Bebauungsplan-
Unterlagen (Begründung, Festsetzungen, städtebauliche Verträge), Sitzungsniederschriften und
-bekanntmachungen. Diese Dokumente sind bereits vor der Verarbeitung öffentlich einsehbar
(Ratsinformationssysteme der Kommunen, siehe `config/sources.yaml`).

Diese Dokumente enthalten **grundsätzlich keine personenbezogenen Daten von Privatpersonen** im
eigentlichen Sinn. Eine Einschränkung gilt es hier aber offen zu benennen: CLAUDE.md schreibt
selbst vor, personenbezogene Daten "auf das Nötige" zu beschränken, *nicht* dass keine vorkommen
– ein Bauherr/Antragsteller ist in aller Regel eine Firma, kann in Einzelfällen aber auch eine
Privatperson sein (z. B. bei einem privaten Bauvorhaben). Es findet **keine automatisierte
Anonymisierung oder Schwärzung** im Code statt; verarbeitet wird der Dokumenttext, wie er in der
öffentlichen Quelle vorliegt.

## Datenfluss & Speicherort

1. **Herunterladen & Vorbereitung – lokal, im Container:** PDFs werden von der jeweiligen
   Kommunen-Website geladen und lokal mit `pdfplumber` in Text umgewandelt
   (`radar/parser.py`). Ein Keyword-Vorfilter (Stufe 1, `config/settings.yaml`:
   `filters.prefilter_keywords`) entscheidet ohne LLM-Aufruf, ob eine Vorlage überhaupt
   weiterverarbeitet wird.
2. **Übertragung an Anthropic:** Nur der Text der vorgefilterten Vorlage wird per HTTPS an die
   Claude API gesendet – bei langen Dokumenten nicht das ganze Dokument, sondern die relevanten
   Seiten (Keyword-Treffer ± `llm.context_pages` Nachbarseiten, `config/settings.yaml`) bzw. bis
   `llm.max_input_chars` Zeichen (CLAUDE.md: "relevante Seiten … statt blindem Abschneiden").
   Die Verbindung läuft über den offiziellen `anthropic`-Python-Client (`anthropic>=1.7`, siehe
   `requirements.lock.txt`), TLS-verschlüsselt; die genaue TLS-Version handeln Client-Bibliothek
   und Anthropic-Server aus (aktuell TLS 1.3 üblich) und wird nicht von dieser Anwendung
   konfiguriert.
3. **Rückgabe & Speicherort:** Die Antwort ist strukturiertes JSON gemäß einem festen
   Pydantic-Schema (`ProjektExtraktion`, `client.messages.parse(output_format=...)` –
   erzwungenes Antwortformat, keine Freitext-Antwort). Dieses Ergebnis – nicht der
   Rohdokumenttext – landet in der lokalen SQLite-Datenbank (`data/radar.db`, im
   Docker-Betrieb ein Volume, siehe `docker-compose.yml`). Ein Cache pro Dokument-Hash
   (`data/llm_cache/`) verhindert wiederholte Aufrufe für unveränderte Dokumente.
4. **Kein Training durch Anthropic:** Nach den aktuellen kommerziellen Nutzungsbedingungen von
   Anthropic ("Commercial Terms of Service") werden über die API übermittelte Ein- und Ausgaben
   nicht standardmäßig zum Training von Anthropic-Modellen verwendet. Diese Aussage stammt aus
   den öffentlichen Vertragsbedingungen des Anbieters, nicht aus dieser Codebasis – vor
   Produktivbetrieb sollte die Rechtsabteilung die zum Vertragsabschluss aktuelle Fassung des
   Anthropic-Vertrags/DPA gegenprüfen (siehe "Offene Punkte" unten).

## API-Key-Verwaltung

Der API-Key wird ausschließlich über die Umgebungsvariable `ANTHROPIC_API_KEY` bereitgestellt:
- Lokal: `.env` (aus `.env.example` kopiert, nie eingecheckt – siehe `.gitignore`).
- Docker: `docker-compose.yml` bindet `.env` per `env_file` in beide Dienste (`dashboard`,
  `scheduler`) ein. Der Key ist **nicht im Docker-Image enthalten** – `.dockerignore` schließt
  `.env` explizit vom Build-Kontext aus, das Image selbst enthält keinen Schlüssel und kann
  daher gefahrlos weitergegeben werden.
- Zugriff im Code ausschließlich über `os.environ`/`python-dotenv` (`radar/config.py`), nirgends
  hartkodiert.

## Zweiter externer Datenfluss: E-Mail-Benachrichtigung (SMTP)

`radar/notifier.py` verschickt optional eine Zusammenfassungs-Mail, wenn ein Lauf neue,
relevante Bauvorhaben findet (Auslöser: `scheduler`-Dienst immer, ein manueller Lauf nur mit
ausdrücklich gesetztem `--notify`/aktivierter Dashboard-Checkbox "E-Mail-Benachrichtigung
senden" – siehe CLAUDE.md, Abschnitt "Referenzquelle ..." bzw. Roadmap Schritt 7).

- **Anbieter:** kein fester Drittanbieter, sondern der SMTP-Relay, den die Firmen-IT selbst in
  `.env` einträgt (`SMTP_SERVER`/`SMTP_PORT`) – z. B. ein eigener Mailserver oder ein Dienst wie
  Microsoft 365/Google Workspace, je nachdem, was das Unternehmen ohnehin einsetzt. Diese
  Konfigurationsentscheidung liegt bei der IT, nicht im Code.
- **Übertragene Daten:** ausschließlich eine kurze Zusammenfassung pro neuem Treffer – Kommune,
  Vorhaben-Titel (die vom LLM extrahierte `projektbezeichnung`), WE-Zahl und ein Link zur
  öffentlichen Quelle (Sitzung/Ratsinformationssystem). **Kein Dokumenttext, keine Anlagen,
  keine personenbezogenen Daten** verlassen an dieser Stelle das System – die Mail enthält
  strukturell nicht mehr als eine Tabellenzeile pro Treffer (siehe `NeuerTreffer` in
  `radar/notifier.py`).
- **Empfänger:** `NOTIFICATION_RECIPIENT` in `.env`, eine oder mehrere kommagetrennte Adressen
  (z. B. Vertrieb und IT gemeinsam) – von der Firmen-IT festgelegt, nicht im Code.
- **Transportweg:** STARTTLS auf dem konfigurierten Port (Normalfall 587), Zugangsdaten
  (`SMTP_USER`/`SMTP_PASSWORD`) wie der Anthropic-Key ausschließlich über `.env`/`env_file`,
  nicht im Docker-Image.
- **Optional und fehlerisoliert:** Fehlt eine der SMTP-Variablen, bleibt die Benachrichtigung
  ohne Fehlermeldung deaktiviert; ein Versandfehler (falscher Login, Server nicht erreichbar)
  wird abgefangen und bricht einen sonst erfolgreichen Lauf nicht ab.

## Aufbewahrung & Löschung (lokale Daten)

Lokal gespeichert werden: SQLite-Datenbank (extrahierte Projektdaten, Quellen-Zustand),
heruntergeladene PDFs (`data/raw/<quelle_id>/`, Cache zu Debug-/Reproduzierbarkeitszwecken) und
der LLM-Ergebnis-Cache (`data/llm_cache/`). Alle drei liegen unter `data/`, im Docker-Betrieb ein
einzelnes Volume – Löschung/Aufbewahrungsfristen sind damit eine reine Dateisystem-Frage für die
Firmen-IT, es gibt keine automatische Löschroutine im Code.

## Offene Punkte für Rechtsabteilung/Datenschutzbeauftragten

Dieses Dokument beschreibt den technischen Ist-Zustand, klärt aber folgende Punkte bewusst
**nicht** abschließend – das ist keine Aufgabe für Code, sondern für die entsprechende Fachstelle:

- **Auftragsverarbeitungsvertrag (AVV/DPA) mit Anthropic, PBC** nach Art. 28 DSGVO, falls die
  Verarbeitung als Auftragsverarbeitung einzustufen ist.
- **Internationale Datenübermittlung:** Anthropic, PBC ist ein US-amerikanisches Unternehmen –
  auch bei rein öffentlichen Kommunaldaten ist zu klären, ob/welche Übermittlungsgarantien
  (z. B. Standardvertragsklauseln) für den Transfer in die USA gelten und ob das für öffentlich
  zugängliche Daten überhaupt einschlägig ist.
- **Aktuelle Aufbewahrungsfristen bei Anthropic** für über die API übermittelte Anfragen/
  Antworten (tagesaktuell im Anthropic-DPA/Trust Center nachlesen, nicht in dieser Datei
  festschreiben, da sich Anbieterbedingungen ändern können).
- **Meldewege bei einem Sicherheitsvorfall** (z. B. kompromittierter `ANTHROPIC_API_KEY`) –
  organisatorisch bei der Firmen-IT festzulegen, technisch lässt sich der Key jederzeit im
  Anthropic-Konsolen-Dashboard widerrufen und in `.env` ersetzen.
