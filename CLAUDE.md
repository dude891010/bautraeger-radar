# Bauträger-Radar – Projektleitfaden

Claude Code liest diese Datei bei jedem Start. Sie ersetzt den ursprünglichen Master-Prompt.

## Ziel
Ein regionaler Küchenhersteller (Hummel Küchen, B2B-Objektgeschäft) will früh von geplanten Wohnbauprojekten
erfahren. **Zielgebiet:** Schleswig-Holstein, Hamburg, Niedersachsen, Bremen, Mecklenburg-Vorpommern und selektiv
Berlin (dort werden nur teilweise Projekte angenommen). Das Tool liest öffentliche Ratsunterlagen vieler Kommunen,
bewertet sie per LLM und zeigt relevante Projekte in einem lokalen Streamlit-Dashboard. Später läuft es als
Docker-Container bei der Firmen-IT. Der Nutzer ist kein Entwickler: einfache, gut lesbare Lösungen bevorzugen.
**Norderstedt ist die Referenzquelle für den MVP, nicht die Grenze des Systems.**

## Architektur: Quellenregister + Adapter
- `config/sources.yaml` enthält eine Zeile pro Ratsinformationssystem (Stadt, Gemeinde, Bezirk) mit Bundesland,
  Systemtyp, URL, Status (`verified` | `candidate`), Tier, optionalen Ausschussmustern und Mindestgröße.
- Pro Systemtyp ein Adapter unter `radar/sources/` (`sessionnet`, `allris`, `oparl`), alle liefern dieselben
  Typen (`Sitzung`, `Dokument`). Parser, Datenbank und Dashboard kennen nur diese Typen, nie ein konkretes System.
- **OParl bevorzugen**, wo eine Kommune es anbietet (offener JSON-Standard, ein Adapter für viele Systeme).
  Sonst HTML-Adapter. Nicht jede OParl-API liefert Dokumente mit, Datei-URLs immer prüfen.
- Aktiviert (`enabled: true`) werden nur Quellen mit `status: verified`; das erzwingt der Config-Loader.
- Neue Quelle aufnehmen: Checkliste in `docs/QUELLENKATALOG.md`. Kein Hardcoding von URLs oder
  Ausschussnamen im Code.
- Priorisierung: Tier A (Hamburg + Umland) zuerst, dann B (übriges Norddeutschland), C (Berlin, selektiv).
  Tiers steuern Reihenfolge und Dashboard-Filter, sie schließen nichts aus. Schwelle `min_units` ist pro Region
  und Quelle überschreibbar (Berlin: offen, mit dem Vertrieb klären).

## Referenzquelle Norderstedt (Stand 19.09.2026, Adapter gegen echte Antworten verifiziert)
- Norderstedt nutzt **Somacos SessionNet 5.5.4 KP1**, nicht ALLRIS. Es gibt kein `si010.asp`.
  Fingerprint im `<head>`: `<meta name="sessionnet" content="V:050504.L:5"/>`.
- Einstieg `https://buergerinfo.norderstedt.de` leitet per Meta-Refresh weiter:
  `/ratsinfo/sessionnet/buergerinfo/` → `default.php` → `info.php`. Die konfigurierte `base_url` zeigt
  direkt auf `.../buergerinfo/`, der Meta-Refresh-Schritt entfällt dadurch.
- Monatskalender: `si0040.php?__cjahr=2026&__cmonat=9&__canz=1&__cselect=0`, Tabelle
  `#smc_page_si0040_contenttable1`. Eine Zeile pro Kalendertag (Zelle `td.smc_fct_day` = Tagesnummer,
  Klasse `smctablesitzung` gibt es hier nicht – das ist die Kalendertabelle, nicht die Tagesordnung).
  Sitzungszelle hat Klasse `silink` (Gremiumsname in `div.smc-el-h`, Detaillink `a.smc_datatype_si` →
  `si0057.php?__ksinr=<ID>` **falls vorhanden**, Uhrzeit/Ort in `ul.smc-detail-list`). Abgesagte
  Sitzungen stehen in einer Zelle mit Klasse `tename` als Text „Die Sitzung des … fällt aus.“ (Gremium
  in Genitivform, nicht verlustfrei in den Nominativ rückführbar – der Ausschussfilter sucht ohnehin
  per Teilstring). Zwischen den Kalenderwochen gibt es Leerzeilen mit leerer Tageszelle.
- Sitzungsdetail: `si0057.php?__ksinr=<ID>`. Ein Direktabruf ohne vorherigen Einstieg lieferte
  „SessionNet Fehlermeldung" → **Cookie-Session nötig**: erst `info.php` mit `requests.Session()`.
  Tagesordnung als Tabelle `table.smctablesitzung`, jede Zeile mit `span.badge` ist ein
  Tagesordnungspunkt („Ö n“ = öffentlich, „N n“ = nichtöffentlich – Letztere werden nicht erfasst).
  Vorlagen-Link `a.smc_datatype_vo` → `vo0050.php?__kvonr=<ID>` (Vorlagen-Nr. z. B. „A 26/0338“ als
  Linktext). Tagesordnungspunkt-Detailseite ist `to0050.php?__ktonr=<ID>` (**nicht** `to0040.php`, wie
  zuvor vermutet) – vom Adapter aktuell nicht benötigt.
- Dokumente: `getfile.php?id=<ID>&type=do`, jeweils als `div.smc-d-el` mit Icon-Kürzel
  (`i.smc-doc-symbol`, z. B. „BM“ = Bekanntmachung, „ÖN“/„N“ = Niederschrift, „VO“ = Vorlage) und
  Titel-Link. Sitzungsweite Dokumente (Einladung, öffentliche Bekanntmachung, Niederschrift) stehen
  außerhalb der Tagesordnungstabelle; je Tagesordnungspunkt stehende Dokumente (Vorlage + alle
  Anlagen) direkt in dessen Zeile. Eine Stichprobe (TOP „A 26/0338“) ergab: die Vorlagen-Detailseite
  `vo0050.php` listet exakt dieselben Dokumente wie `si0057.php` – ein zusätzlicher Abruf pro Vorlage
  ist für die Dokumentliste nicht nötig, spart Requests (Höflichkeit) und Komplexität.
- Nicht jede Sitzung hat eine Detailseite: im Kalender hatte die ASV-Sitzung vom 03.09.2026 einen Link,
  „Kinder- und Jugendbeirat“-Termine keinen. Sitzungen können ausfallen (siehe oben). Fehlende
  Unterlagen sind ein Normalfall und beim nächsten Lauf erneut zu prüfen.
- Umgesetzt in `radar/sources/sessionnet.py`, Tests mit echten (live abgerufenen) Fixtures in
  `tests/fixtures/norderstedt/` und `tests/test_sessionnet.py`, `tests/test_http_client.py`.

## Referenzquelle Uplengen/OParl (Stand 19.09.2026, Adapter gegen echte Antworten verifiziert)
- Ablauf: feste System-URL (JSON) -> `body` (Liste, i. d. R. eine Kommune je Eintrag) -> `meeting`
  (paginiert über `links.next`) -> Detailabruf je Sitzung liefert `agendaItem[]` -> je Punkt optional
  `consultation` -> `paper` (Vorlage) mit `mainFile`/`auxiliaryFile`. Alle IDs sind selbst URLs
  (JSON-LD), deshalb ist `Sitzung.external_id`/`.url` beim OParl-Adapter dieselbe Meeting-URL (anders
  als SessionNets kompakte `__ksinr`) - funktioniert, weil Adapter generisch über viele Hersteller
  hinweg bleiben soll (SD.NET RIM, ALLRIS-Erweiterung, more-rubin, ...), nicht herstellerspezifische
  URL-Muster voraussetzt.
- Die Sitzungsliste ist **nicht chronologisch sortiert** (verifiziert an echten Daten: die zuletzt
  angelegten Termine liegen auf der letzten Seite, aber zeitlich verstreut von Januar bis Dezember
  2026) - der Adapter liest deshalb alle Seiten und filtert selbst nach Datum, statt sich auf eine
  Reihenfolge zu verlassen oder früh abzubrechen.
- Die Meeting-**Liste** liefert je nach Alter/Vollständigkeit mal die volle Tagesordnung mit, mal
  nur die Stammdaten (id/name/start/end/location/organization/keyword) - z. B. bei Sitzungen, die
  erst in der Zukunft liegen und deren Tagesordnung noch nicht feststeht. `list_documents()` fragt
  deshalb den Meeting-**Detail**-Endpunkt (dieselbe `id`-URL) separat und generisch ab, statt sich
  auf angereicherte Listenobjekte zu verlassen.
- Ein Tagesordnungspunkt mit `consultation` gehört zu einer Vorlage (`paper.id` als
  `vorlage_external_id`, `paper.reference` als menschlich lesbare Vorlagen-Nr., z. B. "126/2025");
  `paper.mainFile` + `paper.auxiliaryFile[]` sowie das `resolutionFile`/`auxiliaryFile` des
  Tagesordnungspunkts selbst werden gemeinsam unter dieser einen Vorlage gruppiert (CLAUDE.md:
  "Alle Dokumente einer Vorlage gemeinsam auswerten"). Ein Punkt ganz ohne `consultation`, aber mit
  eigenem `resolutionFile` (z. B. "Genehmigung der Niederschrift"), bekommt eine auf sich selbst
  beschränkte Gruppierung über die Tagesordnungspunkt-ID - kein Bezug über Sitzungen hinweg, weil es
  dafür keine Vorlage/kein Paper gibt.
- **Fund während der Quellensuche, der Schritt 2 betrifft:** die robots.txt der Gemeinde Uplengen
  sperrt PDF-Downloads mit `Disallow: /*.pdf$` - einem verbreiteten Wildcard-Muster, das
  `urllib.robotparser` (Python-Standardbibliothek, bisher in `radar/http_client.py`) nicht versteht
  und fälschlich als erlaubt behandelt hätte. Deshalb ersetzt durch `protego` (schon von Scrapy
  genutzt, versteht `*`/`$`). Gilt für **alle** Adapter, nicht nur OParl - siehe
  `tests/test_http_client.py::test_robots_disallow_understands_wildcard_and_end_anchor`.
- Weil bei Uplengen jede Vorlage ausschließlich als PDF vorliegt, macht die robots.txt-Sperre die
  Quelle für den echten Betrieb wertlos (jeder Download würde korrekt verweigert). In
  `config/sources.yaml` deshalb `status: verified` (Struktur echt geprüft), aber `enabled: false`.
  PDF-Downloads von dort wurden dementsprechend **nicht** live abgerufen, auch nicht einmalig für
  Fixtures - der gemeinsame `HttpClient.download_pdf()` ist bereits an Norderstedt live verifiziert
  und wird hier nur mit synthetischen PDF-Bytes getestet. Für den echten Betrieb muss eine
  OParl-Quelle im Zielgebiet ohne PDF-Sperre gefunden werden (siehe docs/QUELLENKATALOG.md).
- Umgesetzt in `radar/sources/oparl.py`, Tests mit echten (live abgerufenen) Fixtures in
  `tests/fixtures/uplengen/` und `tests/test_oparl.py`.

## Referenzquelle Hamburg (Stand 19.09.2026, zwei bewusst getrennte Adapter)
Die offiziellen ALLRIS-Seiten aller 7 Hamburger Bezirksversammlungen (Altona, Bergedorf,
Eimsbüttel, Hamburg-Mitte, Hamburg-Nord, Harburg, Wandsbek) sperren Crawler pauschal
(`Disallow: /`, offenbar eine zentrale Dataport-Hosting-Vorlage) - für uns nicht direkt nutzbar.
Deshalb zwei Ersatzquellen mit unterschiedlicher Abdeckung, **bewusst als eigene Systemtypen/eigene
Dateien getrennt**, damit die inoffizielle Quelle später unabhängig ausgetauscht/angepasst werden
kann, ohne die offizielle zu berühren:

- **`hamburg_transparenz`** (`radar/sources/hamburg_transparenz.py`): offizielle CKAN-API des
  Transparenzportals Hamburg, Register `bauleitplaene`. Gesetzlich vorgeschrieben (Hamburgisches
  Transparenzgesetz), offene Lizenz, echte PDF-Dokumente (Begründung, Festsetzungen,
  Bekanntmachung) direkt hostet auf weiteren Hamburg-Domains ohne eigene robots.txt
  (`additional_hosts`, siehe unten). **robots.txt-Sonderfall:** `suche.transparenz.hamburg.de`
  sperrt `/api/` pauschal, dokumentiert und lizenziert dieselbe API aber selbst ausdrücklich zur
  automatisierten Nutzung - deshalb `ignore_robots_txt: true` für diese eine Quelle (Feld erzwingt
  eine Begründung in `notes`, siehe `radar/config.py`). **Deckt nur die letzte Verfahrensstufe ab**
  (Satzungsbeschluss, im HmbGVBl veröffentlicht) - frühere, für den Vertrieb oft wertvollere Stufen
  fehlen hier.
- **`bv_hh`** (`radar/sources/bv_hh.py`, sieben Quellen `bv_hh_*` in `config/sources.yaml`, eine je
  Bezirk): inoffizielles Community-Portal bv-hh.de (github.com/bv-hh/bv-hh), spiegelt dieselben
  ALLRIS-Daten aller 7 Bezirke mit offener robots.txt. Deckt zusätzlich die frühen Verfahrensstufen
  ab (Aufstellungsbeschluss, Auslegung), weil es direkt die Bezirksversammlungs-Drucksachen zeigt -
  dafür ohne offiziellen Betreiber und ohne Verfügbarkeitsgarantie (Hobby-Projekt). **Liefert keine
  PDF-Anlagen** (an mehreren echten Drucksachen unterschiedlichen Typs geprüft: die
  "Anhänge"-Sektion war überall leer) - nur den bereits extrahierten Haupttext je Drucksache
  (JSON-LD `articleBody`). Deshalb WE-Zahlen aus separaten Anlagen (Begründung, städtebaulicher
  Vertrag) darüber NICHT erreichbar, unklare Fälle entsprechend häufiger als "prüfen" markiert.

**Cross-cutting Änderungen, die dabei nötig wurden (betreffen alle Adapter, nicht nur Hamburg):**
- `radar/config.py`/`radar/http_client.py`: `Source.additional_hosts` - eine Quelle darf jetzt von
  mehreren Hosts laden (ein offizielles Open-Data-Ökosystem verteilt API und Dateiablage oft auf
  verschiedene Domains). Jeder zusätzliche Host bekommt seine eigene, unabhängig geprüfte
  robots.txt; `Source.ignore_robots_txt` gilt nur für den primären Host (`base_url`).
  `Source.request_delay_seconds` überschreibt `scraper.request_delay_seconds` pro Quelle (für
  strengere `Crawl-Delay`-Vorgaben einzelner Hosts).
- `radar/sources/base.py`/`radar/parser.py`/`radar/scraper.py`: `Dokument.text` - eine Quelle darf
  Text direkt mitliefern statt einer PDF-URL (kein Download, kein `HttpClient.download_pdf()`).
  `radar/parser.analyze_documents()`/`documents_cache_key()` nehmen dafür `Path | Textinhalt`
  entgegen (`Textinhalt` = einfacher Text-Wrapper, `ocr_noetig` immer `False`). Ausgelöst durch
  bv-hh: die einzige bisherige Alternative wäre gewesen, den fehlenden PDF-Anlagen-Text künstlich
  in eine Fake-PDF-Datei zu verpacken - stattdessen ein sauberer zweiter Eingabetyp.
- Umgesetzt und getestet: `tests/test_hamburg_transparenz.py` (Fixtures
  `tests/fixtures/hamburg_transparenz/`), `tests/test_bv_hh.py` (Fixtures
  `tests/fixtures/bv_hh/`, gegen die echte Bergedorf-Instanz geprüft - dieselbe Plattform bedient
  alle 7 Bezirke identisch), neue Tests für `additional_hosts`/`ignore_robots_txt`/
  `request_delay_seconds` in `tests/test_http_client.py`/`tests/test_config.py`, neue Tests für
  `Textinhalt` in `tests/test_parser.py`/`tests/test_scraper.py`. Beide Quellen `status: verified`,
  aber `enabled: false` - vor der Aktivierung mit dem Vertrieb abstimmen (robots.txt-Auslegung bei
  hamburg_transparenz, Verlässlichkeit/Datenqualität bei bv_hh).

## Referenzquelle ALLRIS/Hamburger Umland (Stand 19.09.2026, status: blocked)
Für die Hamburger-Rand-Kommunen (Tier A) einen generischen ALLRIS-Adapter gesucht. Alle
robots.txt-offenen Kandidaten im Umland (Wedel, Amt Siek, Amt Hohe Elbgeest, Amt Pinnau) laufen auf
**"ALLRIS net" Version 4.1.7** (Apache Wicket) statt dem im Quellenkatalog vermuteten klassischen,
`.asp`-basierten ALLRIS - eine komplett AJAX-/session-basierte Oberfläche: selbst der beim ersten
Aufruf angezeigte Kalendermonat wird nicht serverseitig mitgeliefert, sondern erst durch einen
nachgelagerten, zustandsbehafteten AJAX-Aufruf befüllt (sitzungsgebundene Wicket-Komponenten-IDs,
keine einfachen GET-Parameter). Details unter docs/QUELLENKATALOG.md, Abschnitt "ALLRIS net 4.x".
Geesthacht (ebenfalls im Umland) zusätzlich durch ein explizites Bot-Abwehr-Gate (ALTCHA-Captcha)
vor ALLRIS geschützt.

**Bewusste Architekturentscheidung: kein Headless-Browser.** Ein Adapter für ALLRIS net 4.x bräuchte
entweder eine Nachbildung des Wicket-AJAX-Protokolls (aufwendig, bricht potenziell bei jedem
ALLRIS-Update) oder einen Headless-Browser - Letzteres explizit abgelehnt, um das System
leichtgewichtig, stabil und ohne Browser-Abhängigkeit wartbar zu halten (gilt für alle Quellen,
nicht nur ALLRIS). `radar/sources/allris.py` bleibt deshalb ein Stub, `STATUSES` in `radar/config.py`
um `blocked` erweitert (geprüft, aber technisch/rechtlich nicht nutzbar - nie aktivierbar, wie
`candidate`). `config/sources.yaml` enthält die Quelle "wedel" mit `status: blocked` zur
Dokumentation des Funds. Reaktivieren, falls eine klassische, GET-basierte ALLRIS-Installation im
Zielgebiet auftaucht, oder falls die Architekturentscheidung gegen einen Headless-Browser revidiert
wird.

## Fachliche Regeln
- **Gremium:** je Kommune anders benannt (Norderstedt „Ausschuss für Stadtentwicklung und Verkehr", Hamburg-Bergedorf
  „Stadtentwicklungsausschuss", „Fachausschuss für Bauangelegenheiten" …). Erkennung über Regex-Muster
  (`filters.default_committee_patterns`, pro Quelle überschreibbar), Logik in `radar/committees.py`.
  Für Bauvorhaben zuständig sind Stadtentwicklungs-, Planungs- und Bauausschüsse.
- **Bezugsgröße:** Miet- plus öffentlich geförderte Wohneinheiten ≥ Schwelle (Default 6). Mischprojekte
  (Miete + Eigentum) zählen, wenn allein der Miet-/geförderte Anteil die Schwelle erreicht. Gerade in Hamburg
  („Drittelmix") sind Mischprojekte die Regel.
- **Ausschluss:** reine Eigentumswohnungen (ETW), Gewerbe ohne Wohnen, Garagen, reine Einfamilienhäuser.
- **Unklare Fälle nie stillschweigend verwerfen.** Fehlt die Anzahl oder die Wohnform, wird das Projekt mit Flag
  „prüfen" behalten (`unknown_*_policy: flag`). Ein Fehlalarm ist für den Vertrieb billiger als ein verpasstes Projekt.
- Felder pro Vorhaben: Projektbezeichnung, Adresse (Straße, Hausnummer, Flurstück, Ort), WE gesamt, davon Miet-WE,
  davon öffentlich geförderte WE (Anzahl und/oder ja/nein), Quote gefördert, Ausführungszeitraum/Baubeginn,
  Antragsteller/Bauherr/Investor/Vorhabenträger, Kurzfassung (1–2 Sätze).
- Zusätzlich (wichtig für den Vertrieb):
  - Herkunft: `bundesland`, `kommune`, `quelle_id`, `gremium`, `sitzungsdatum`, Vorlagen-Nr., URL.
  - `wohnform`: `MIETE | GEFOERDERT | EIGENTUM | GEMISCHT | SONDERWOHNFORM | UNKLAR`. Sonderwohnformen
    (Studierendenwohnen, betreutes Wohnen, Mikroapartments) nicht verwerfen, sondern markieren; ob sie
    relevant sind, entscheidet der Vertrieb.
  - `verfahrensstand`: `VORBERATUNG | AUFSTELLUNGSBESCHLUSS | AUSLEGUNG | SATZUNGSBESCHLUSS | BAUGENEHMIGUNG | UNKLAR`
    (ein B-Plan-Aufstellungsbeschluss liegt oft Jahre vor dem Küchenbedarf, das Dashboard muss das zeigen).
  - `konfidenz` (0–1), `evidenz` (wörtliches Zitat ≤ 200 Zeichen, auf dem die WE-Zahl beruht) und `seite`.
- Begriffe für geförderten Wohnraum sind regional verschieden (Förderweg, Drittelmix, Belegungsbindung,
  Wohnberechtigungsschein, Sozialwohnungsquote). Stichwortliste und Prompt entsprechend pflegen.
- Wohneinheiten stehen oft nur in Anlagen (Begründung, städtebaulicher Vertrag), nicht in der Beschlussvorlage.
  Alle Dokumente einer Vorlage gemeinsam auswerten.

## Datenmodell (Schritt 4)
- Tabellen `quellen` (inkl. Zustand: letzter Erfolg, letzter Fehler, Anzahl Sitzungen), `sitzungen`, `vorlagen`,
  `dokumente` (mit SHA-256), `projekte`, `projekt_vorlage` (n:m), `status_log`.
- Ein Projekt taucht in mehreren Gremien und Sitzungen auf. Dedup doppelt:
  1. Vorlage: `quelle_id` + externe Vorlagen-ID + Dokument-Hash (geänderte Anlage → erneut analysieren),
  2. Projekt: Zuordnung über normalisierte Adresse/B-Plan-Nr./Bezeichnung, im Zweifel manuell zusammenführen.
- SQLite mit `PRAGMA journal_mode=WAL`, `foreign_keys=ON`. Manuell gepflegte Felder (Status, Notizen) werden von
  späteren Läufen **nie überschrieben**. Schema-Version in `PRAGMA user_version`, kleine SQL-Migrationen.

## Scraping-Regeln
- Ein `requests.Session` pro Quelle und Lauf (Cookies!), `urllib3.Retry` mit Backoff für 429/5xx, immer Timeouts
  (connect + read), nie ein Request ohne Timeout.
- Höflich: identifizierbarer User-Agent mit Kontaktadresse (`SCRAPER_CONTACT`), ≥ 2 s zwischen Requests **pro Host**,
  `robots.txt` und Nutzungsbedingungen je Quelle prüfen und beachten. Pro Host strikt seriell; Parallelität höchstens
  über verschiedene Hosts (`max_parallel_hosts`, Start mit 1).
- `robots.txt`-Prüfung nutzt `protego`, **nicht** `urllib.robotparser`: Letzterer versteht die gängigen
  `*`/`$`-Wildcards (z. B. `Disallow: /*.pdf$`) nicht und hätte einen so gesperrten Pfad fälschlich als erlaubt
  behandelt (gefunden bei der Prüfung einer echten OParl-Quelle, siehe "Referenzquelle Uplengen/OParl").
- **Quellen sind voneinander isoliert:** Ein Fehler in einer Quelle stoppt nie den Lauf. Pro Quelle Zustand
  protokollieren (Erfolg, Fehler, Anzahl gefundener Sitzungen) und im Dashboard als Quellen-Monitoring zeigen.
- HTML-Parsing mit BeautifulSoup + lxml. Stabile Anker bevorzugen (URL-Muster, IDs, Linktexte) statt fragiler
  CSS-Pfade oder Tabellenpositionen.
- **Fail loud:** Findet der Kalender-Parser bei einem Monat mit Sitzungen plötzlich 0 Treffer, ist das ein
  Layoutwechsel → Fehler + Log, kein stilles „nichts gefunden". Vor dem Parsen prüfen, ob die Seite zum erwarteten
  Systemtyp passt (Fingerabdruck), damit eine geänderte Installation auffällt.
- Downloads: nur Host der Quelle, Größenlimit, `Content-Type` und PDF-Magic-Bytes (`%PDF`) prüfen, Dateinamen aus IDs
  bilden (kein Path-Traversal), Cache unter `data/raw/<quelle_id>/` mit Hash.
- Idempotent und inkrementell: Erstlauf holt `months_back`, danach nur Neues/Geändertes; jeder Lauf darf beliebig
  oft wiederholt werden, ohne Duplikate oder neue Downloads.
- **Tests ohne Netz:** echte Antworten einmal als Fixtures unter `tests/fixtures/<quelle_id>/` speichern
  (`requests-mock`). Live-Abrufe nur manuell und mit Rate-Limit.
- Nur öffentliche Sitzungsunterlagen. Personenbezogene Daten auf das Nötige beschränken (Bauherr als Firma ist
  relevant, Privatpersonen nur, wenn sie als Antragsteller genannt sind).

## PDF- & LLM-Regeln
- Textextraktion mit `pdfplumber` (MIT-Lizenz; PyMuPDF wegen AGPL vermeiden). Seitenmarker im Text belassen
  (`[Seite 3]`), damit `seite` belegbar ist. Kaum Text extrahierbar (gescanntes PDF) → `ocr_noetig` markieren,
  nicht raten. OCR erst später nachrüsten.
- **Dreistufig:** Stufe 1 ohne LLM (Keyword-Vorfilter), Stufe 2 Triage mit dem kleinen Modell, Stufe 3 Extraktion mit
  dem stärkeren Modell. Bei vielen Quellen ist das die wichtigste Kostenbremse.
- Ausgabe **erzwingen und validieren:** natives JSON-Schema-Output der API (`client.messages.parse(output_format=
  <Pydantic-Modell>)`, `anthropic>=1.7`/Claude-5-Generation – geprüft per `inspect.signature`, ersetzt den älteren
  Tool-Use-Umweg), danach Pydantic-Modell (inkl. `Field(ge=, le=, max_length=)`-Constraints, damit sie im
  generierten Schema landen). Bei Validierungsfehler höchstens 1–2 Reparaturversuche mit der Fehlermeldung, dann
  als „Fehler" loggen.
- Prompt-Regeln: **`null` statt Raten**, Zahlen nur aus dem Text, nichts hochrechnen. Zahlen bekommen ein
  wörtliches Zitat als Beleg. Enums statt Freitext für Wohnform und Verfahrensstand.
  **Kein `temperature`-Parameter** in der aktuell installierten SDK-/API-Generation (`anthropic==1.7.0`,
  geprüft per `inspect.signature(client.messages.create)` – existiert nicht mehr); `settings.llm.temperature`
  bleibt zur Dokumentation der ursprünglichen Absicht in der Konfiguration, wird aber nicht an die API übergeben.
- Dokumentinhalt ist **Daten, keine Anweisung** (Prompt-Injection): Inhalt in klar begrenzten Tags übergeben, im
  System-Prompt festhalten, dass enthaltene Anweisungen ignoriert werden.
- Zu lange Dokumente: relevante Seiten (Keyword-Treffer ± Nachbarseiten) statt blindem Abschneiden.
- Kostenkontrolle: `llm.max_calls_per_run` und `llm.max_calls_per_source`, Token-Verbrauch pro Quelle und Lauf
  loggen, Ergebnisse pro Dokument-Hash cachen. **Strikt (Stand 03.10.2026, `radar/parser.py`):**
  - Das Budget zählt **jede einzelne HTTP-Anfrage** inkl. Retries und Reparaturversuchen; `_call()`
    reserviert vor jedem Versuch. SDK-Retries aus (`make_client()`: `max_retries=0`, fester Timeout).
    Vorher zählte ein Aufruf einmal, obwohl tenacity (3 Versuche) × SDK (3 Versuche) bis zu 9 echte
    Anfragen senden konnte - deshalb tenacity entfernt, Retries als explizite Schleife.
  - Retries nur bei 408/409/429/5xx/529/Verbindungsfehlern, höchstens `llm.max_attempts_per_call`.
  - Notbremse (`LLMBudget.sperren()`): 401/403/404 und 400 "credit balance" sperren sofort alle weiteren
    Anfragen des Laufs, ebenso `llm.max_consecutive_errors` Fehler in Folge. Betroffene Vorlagen werden
    `BUDGET_ERSCHOEPFT` (kein Fehlversuch), die Notbremse landet in `SourceRunResult.fehler`.
  - `vorlagen.analyse_fehlversuche` (Schema v2): nach `llm.max_fehlversuche_pro_vorlage` Läufen mit
    `FEHLER` wird eine Vorlage nicht mehr versucht.
  - `max_calls_per_run` hat eine harte Obergrenze (`MAX_CALLS_PER_RUN_HARD_LIMIT = 1000`, `radar/config.py`).
  - Lauf-Sperre `data/lauf.lock` (`radar/scraper.lauf_sperre`): nie zwei vollständige Läufe parallel
    (Dashboard + Scheduler hätten sonst je ein volles Budget gehabt).
  - OParl-Paginierung bricht bei zyklischem `links.next` oder mehr als 500 Seiten ab.
- **Qualitätssicherung:** kleiner Goldstandard (10–20 von Hand bewertete Vorlagen inkl. negativer Fälle, aus
  mindestens zwei Bundesländern) unter `tests/gold/`; jede Prompt-Änderung wird dagegen gemessen.
- Alle Prompts als Dateien unter `config/prompts/` (versioniert), nicht im Python-Code.
- Datenschutz/IT: Unterlagen sind öffentlich, gehen aber an eine externe API. Für die Firmen-IT dokumentieren
  (Anbieter, Datenkategorien, Speicherort).

## Architektur & Konventionen
- Python 3.12, Paket `radar/` (`config`, `committees`, `sources/`, `scraper`, `parser`, `database`), Dashboard `app.py`.
  Globale Einstellungen `config/settings.yaml`, Quellen `config/sources.yaml`, Secrets nur in `.env`.
- Kleine, testbare Funktionen; Netzwerk und LLM hinter dünnen Schnittstellen, damit Tests sie ersetzen können.
- `logging` statt `print`, Lauf-Zusammenfassung am Ende (je Quelle: Sitzungen, Vorlagen, Treffer, Fehler, Kosten).
- Dashboard: Filter nach Bundesland, Ort, Tier, Wohnform, Verfahrensstand, Status; Scraper-Lauf im Hintergrund
  (Thread/Subprozess) mit Fortschrittsanzeige. Streamlit hat keine Authentifizierung → im Container nur hinter
  Reverse Proxy/SSO der IT oder mit Basic Auth. Export als CSV/Excel für den Vertrieb.
- Zusätzlich zum Button: geplanter Lauf (wöchentlich, Cron/Container-Scheduler), optional E-Mail bei neuen Treffern.
- Docker (Schritt 7): `python:3.12-slim`, Nicht-Root-Nutzer, `data/` als Volume (SQLite + Cache), Healthcheck,
  Secrets per Umgebung, gepinnte Abhängigkeiten.

## Roadmap
1. ✅ Projektstruktur, Konfiguration, Quellenregister, Adapter-Schnittstelle, Requirements
2. ✅ Gemeinsamer HTTP-Client + SessionNet-Adapter (Norderstedt): Session, Kalender, ASV-Sitzungen, Tagesordnung/Vorlagen,
   PDF-Download, Fixtures + Tests. `radar/http_client.py` (Session, Retry, Timeouts, robots.txt, Rate-Limit,
   PDF-Validierung + Cache), `radar/sources/sessionnet.py` (Kalender, Sitzungen, Tagesordnung, Dokumente) –
   verifiziert gegen echte Live-Antworten, Fixtures unter `tests/fixtures/norderstedt/` – und `radar/scraper.py`
   (Lauf-Orchestrierung: aktive Quellen laden, Ausschussfilter anwenden, Downloads auslösen,
   Lauf-Zusammenfassung je Quelle, Fehlerisolation pro Quelle). Noch kein echter End-to-End-Lauf gegen die
   Produktivseite gefahren (nur einzelne, gezielte Live-Abrufe zur Verifikation) und keine Persistenz
   (Quellen-Zustand landet nur im Log, nicht in einer Datenbank) – das kommt mit Schritt 4.
3. 🟡 Parser: PDF-Text, Vorfilter, LLM-Triage und -Extraktion, Goldstandard. `radar/parser.py` vollständig
   implementiert (pdfplumber-Extraktion mit `ocr_noetig`, Keyword-Vorfilter mit Nachbarseiten-Excerpt,
   Pydantic-Modelle `ProjektExtraktion`/`TriageErgebnis`, `LLMBudget`, dateibasierter `ResultCache` pro
   Dokument-Hash, `analyze_documents()` als Zustandsmaschine), Prompts unter `config/prompts/`, Goldstandard
   (12 synthetische, klar gekennzeichnete Testfälle aus 6 Bundesländern) unter `tests/gold/faelle.yaml` mit
   manuellem Auswertungsskript `scripts/run_goldstandard.py`. Gegen ein Fake-Anthropic-Objekt vollständig
   getestet (kein Netzzugriff in `pytest`). **Noch offen:** ein echter Live-Aufruf schlägt aktuell mit
   `anthropic.BadRequestError: Your credit balance is too low` fehl (Account-Guthaben, kein Code-Fehler – die
   Anfrage selbst kam korrekt formatiert bei der API an); Goldstandard-Lauf und echte Qualitätsmessung stehen
   deshalb noch aus, sobald Guthaben verfügbar ist.
4. ✅ Datenbank: Schema, Dedup, Statuspflege, Quellen-Monitoring. `radar/database.py`: SQLite mit
   `PRAGMA journal_mode=WAL`/`foreign_keys=ON`, Schema-Version in `PRAGMA user_version` mit versionierter
   Migration. Tabellen `quellen` (Zustand/Monitoring), `sitzungen`, `vorlagen`, `dokumente`, `projekte`,
   `projekt_vorlage` (n:m), `status_log`. Zweistufige Dedup: Vorlage über `quelle_id` + externe Vorlagen-ID +
   Dokument-Hash (`upsert_vorlage`, geänderte Anlage -> neuer Datensatz, erneute Analyse), Projekt über eine
   normalisierte Adresse/Bezeichnung innerhalb derselben Quelle (`upsert_projekt`/`normalize_projekt_key`,
   im Zweifel eigener Datensatz statt Rateverknüpfung). `status`/`notizen` werden von `upsert_projekt()` nie
   angefasst, nur `set_projekt_status()`/`set_projekt_notizen()` schreiben sie, Statuswechsel gehen in
   `status_log`. Bewusst lose gekoppelt von `radar.scraper`/`radar.parser` (nimmt deren Typen entgegen, ruft
   sie nicht selbst auf) – die Verdrahtung übernimmt `radar.scraper.run_full()` (siehe unten).

   **Verdrahtung (Scraper -> Parser -> Datenbank):** `radar/scraper.py` hat jetzt zwei Einstiegspunkte.
   `run()`/`run_source()`/`_collect()` bleiben reines Scraping ohne Netzzugriff zu Anthropic oder Datenbank
   (für Trockenläufe). Neu: `run_full()`/`run_source_full()`/`_collect_and_persist()` – der vollständige Lauf:
   speichert Sitzungen/Vorlagen/Dokumente, gruppiert Dokumente je Sitzung nach `vorlage_external_id`
   (sitzungsweite Dokumente ohne Vorlagen-Bezug wie Einladung/Bekanntmachung werden gespeichert, aber nicht
   analysiert), lässt jede noch nicht abgeschlossen analysierte Vorlage durch `radar.parser.analyze_documents`
   laufen und schreibt das Ergebnis. Ein `LLMBudget` gilt über den ganzen Lauf. Dabei zwei Korrekturen
   gegenüber der ursprünglichen Schritt-4/-3-Umsetzung, die beim Verdrahten auffielen:
   - `upsert_vorlage()` prüfte bisher nur den Dokument-Hash, nicht den `analyse_status`: eine Vorlage, deren
     Analyse nur an einem erschöpften Budget oder einem Fehler scheiterte (`BUDGET_ERSCHOEPFT`/`FEHLER`),
     wurde beim nächsten Lauf fälschlich als "schon erledigt" übersprungen und nie wieder versucht. Jetzt
     gelten nur `RELEVANT`/`NICHT_RELEVANT`/`OCR_NOETIG` als abgeschlossen, alles andere wird erneut versucht.
   - `analyze_documents()` fing Fehler bei der PDF-Textextraktion nicht ab; ein beschädigtes PDF hätte den
     ganzen Lauf gestoppt. Jetzt wie die LLM-Aufrufe abgefangen und als `AnalyseStatus.FEHLER` protokolliert.
   - `Dokument` hat jetzt zusätzlich `vorlage_nr` (menschlich lesbare Vorlagen-Nr., z. B. "A 26/0338" – stand
     schon vorher im geparsten SessionNet-HTML, wurde aber nicht in die Datenbank übernommen).
   116 Tests insgesamt.

   **Erster echter Produktiv-Lauf (19.09.2026, über das Dashboard "Lauf jetzt starten"):**
   `run_full()` gegen die echte Norderstedt-Seite bestätigt End-to-End funktionsfähig – 106
   Sitzungen im 5-Monats-Fenster (`months_back`/`months_ahead`), 6 davon zum Ausschuss für
   Stadtentwicklung und Verkehr passend, 26 Vorlagen, 127 Dokumente heruntergeladen (~150 MB,
   `data/raw/norderstedt/`), alle korrekt in der Datenbank persistiert. Stufe-1-Vorfilter
   (Keyword, kein LLM) hat 9 von 26 Vorlagen kostenlos als `NICHT_RELEVANT` aussortiert – die
   Kostenbremse greift wie vorgesehen. Die verbleibenden 17 scheiterten an
   `anthropic.BadRequestError: Your credit balance is too low` (bereits unter Schritt 3 bekannt,
   Account-Guthaben, kein Code-Fehler) – 0 Treffer deshalb erwartungsgemäß, nicht symptomatisch
   für einen Bug. Ein einzelner Download wurde korrekt wegen Größenlimit abgelehnt
   (`scraper.max_pdf_size_mb: 25`, funktioniert wie konfiguriert). Damit ist die Scraping- und
   Persistenz-Seite der Pipeline erstmals live gegen die Produktivseite verifiziert; die
   LLM-Extraktions-Qualität (Goldstandard, echte Treffer) steht weiterhin aus, bis Guthaben
   verfügbar ist.
5. ✅ Streamlit-Dashboard. `app.py`: Tabs "Projekte" (Filter nach Bundesland, Ort, Tier, Wohnform,
   Verfahrensstand, Status – Tier kommt aus der Quellenkonfiguration, nicht aus der Datenbank; `st.data_editor`
   zum Bearbeiten von Status/Notizen, schreibt über `database.set_projekt_status()`/`set_projekt_notizen()` –
   nie über `upsert_projekt()`, damit manuelle Pflege nie überschrieben wird; CSV-/Excel-Export für den
   Vertrieb; Vorkommen-Ansicht je Projekt über `list_vorlagen_fuer_projekt()`) und "Quellen-Monitoring"
   (`list_quellen()`). Lauf im Hintergrund als **Subprozess** (`python -m radar.scraper`, nicht Thread – vermeidet
   SQLite-Threading-Fragen sauber), Log-Tail zum Fortschritt, Auto-Refresh per `st.rerun()`-Schleife alle 2s
   während ein Lauf aktiv ist. Reine Datenlogik (Filtern, Diff von Bearbeitungen, Export) steht als pure
   Funktionen ohne `st.*`-Aufrufe oben im Modul, `main()` (alles mit `st.*`) läuft nur unter `streamlit run`
   (`if __name__ == "__main__"` – Streamlit führt das Skript mit `__name__ == "__main__"` aus), damit die pure
   Logik ohne laufende Streamlit-Session testbar ist. Manuell im Browser mit Testdaten geprüft (Filter, Editor,
   Export, Quellen-Tab) – Screenshots nicht aufbewahrt, Testdaten danach wieder gelöscht. Excel-Export braucht
   `openpyxl` (zu requirements.txt hinzugefügt). 15 neue Tests für die pure Datenlogik, 131 insgesamt.

   **Nachträglich ergänzt: Demo-Modus** (`radar/demo_data.py`, auf Nutzerwunsch für Präsentationen
   beim Küchenhersteller, solange kein Anthropic-Guthaben für einen echten Lauf verfügbar ist,
   siehe Schritt 3). Sidebar-Schalter "Demo-Daten anzeigen" in `app.py` zeigt 9 synthetische,
   frei erfundene Beispielprojekte über alle sechs Zielregionen (12-85 WE, Status "Neu" bis
   "Beschlossen", ein bewusst unklarer Fall mit `wohnform=UNKLAR` und fehlender Förderzahl – zeigt
   die "prüfen"-Regel im Dashboard). Schreibt/liest dafür eine eigene Datei `data/demo/demo.db`
   (nie `data/radar.db`) über dieselbe `radar.database`-Schicht und dieselben
   `upsert_sitzung()`/`upsert_vorlage()`/`upsert_projekt()`-Funktionen wie ein echter Lauf, daher
   genauso idempotent. `ensure_demo_seeded()` befüllt sie nur beim allerersten Aktivieren (leere
   Demo-DB) – im Demo-Modus vorgenommene Status-/Notizen-Änderungen bleiben danach erhalten, bis
   `scripts/seed_demo_data.py` manuell zurücksetzt. Tier-Zuordnung der Demo-Quellen (die es in
   `config/sources.yaml` bewusst nicht gibt) kommt aus `demo_data.DEMO_TIER_BY_QUELLE`, gemischt in
   `app.py`s `tier_by_quelle`. `config/settings.yaml`s `dashboard.statuses` um "Beschlossen"
   ergänzt (bisher fehlte ein Status für abgeschlossene Verfahren). `data/demo/*.db` zum
   `.gitignore` hinzugefügt (wie die echte DB nie einchecken).
   **Dabei gefundener, unabhängig vom Demo-Modus bestehender Bug:** `app.py`s
   `_projekt_label()`-Fallback für Projekte ohne `projektbezeichnung` (CLAUDE.md: unklare Fälle
   werden behalten) nutzte `wert or "(ohne Bezeichnung)"` – pandas wandelt ein fehlendes `None` in
   einer gemischten Objekt-Spalte aber in `float("nan")` um, und `nan` ist in Python wahr, sodass
   der Fallback nie griff und wörtlich "nan (Ort)" angezeigt wurde. Jetzt ein expliziter
   `pd.isna()`-Check. Manuell im Browser mit aktivem Demo-Modus verifiziert (Projekte-Tabelle,
   Tier-/Status-Filter, Export, Vorkommen-Auswahl, Quellen-Tab). 5 neue Tests
   (`tests/test_demo_data.py`), 220 insgesamt.

   **Nachträglich ergänzt: Hummel-Küchenwerk-Branding.** `.streamlit/config.toml` setzt
   `[theme]` (`primaryColor = "#FF6600"`, warme Creme als `secondaryBackgroundColor`) – deckt
   die von Streamlit selbst eingefärbten Elemente ab (primäre Buttons, Checkboxen, aktiver Tab).
   `app.py`s `HUMMEL_CSS`-Konstante ergänzt, was das Theme allein nicht kann: Hover-Zustand
   `#E65100` auf Buttons, orange gefüllte Auswahl-Chips in den Multiselect-Filtern, abgerundete
   Ecken/Schatten auf den Metrik-Kacheln und Tabellen, Marken-Akzent am linken Rand der Hinweis-
   boxen. `st.logo()` (Streamlit 1.64, verfügbar seit 1.36) zeigt `assets/hummel_logo.png` oben
   in der Sidebar, `st.set_page_config(page_icon=...)` nutzt dieselbe Datei als Browser-Tab-Icon.
   Neue, pure `compute_kpis()`-Funktion speist vier KPI-Kacheln oberhalb der Tabs (Projekte,
   Wohneinheiten gesamt, davon gefördert, offen/"Neu"+"In Prüfung") – liefert `None` statt einer
   irreführenden 0, wenn eine Summe mangels Daten nicht gebildet werden kann (CLAUDE.md: nicht
   raten/hochrechnen), die Oberfläche zeigt dafür "–". Manuell im Browser mit Demo-Daten
   verifiziert (Logo, Theme-Farben, KPI-Kacheln, Chip-Farbe, Button-Hover, Tab-Unterstreichung).
   3 neue Tests für `compute_kpis()`, 223 insgesamt.

   **Nachträglich ergänzt: Freitextsuche.** Sidebar-Feld "Suche" (`st.text_input`, oberhalb der
   Dropdown-Filter, da erfahrungsgemäß der meistgenutzte Zugriff bei wachsender Projektzahl) -
   neue, pure Funktion `apply_search()` durchsucht `projektbezeichnung`, `strasse`, `hausnummer`,
   `ort`, `kommune` und `antragsteller` (Groß-/Kleinschreibung egal), leerer Suchtext lässt die
   Tabelle unverändert. Läuft vor `apply_filters()` in der Filterkette
   (`apply_filters(apply_search(df, suche), ...)`), damit Suche und Dropdown-Filter sich
   kombinieren statt sich gegenseitig zu ersetzen. Nutzt `fillna("")` statt eines `None`-Checks,
   weil pandas ein fehlendes Feld in einer gemischten Objekt-Spalte zu `float("nan")` macht (siehe
   den `_projekt_label()`-Bugfix oben) - `str.contains` bräche daran sonst ab, betrifft hier vor
   allem Projekte mit fehlender `projektbezeichnung` (CLAUDE.md: "unklare Fälle nie stillschweigend
   verwerfen" - die müssen trotzdem über Adresse/Bauherr auffindbar bleiben). Manuell im Browser
   mit Demo-Daten verifiziert (Suche nach Ortsteil, Straße und Antragsteller). 6 neue Tests, 229
   insgesamt.

   **Nachträglich ergänzt: Warn-Badge für unsichere Fälle + Statusverlauf.**
   - Neue, pure Funktion `add_pruef_badge()` (`app.py`) ergänzt eine Spalte `pruefen` mit dem
     Badge "⚠ prüfen" für Projekte mit `konfidenz < KONFIDENZ_SCHWELLE` (0.6, bewusst eine grobe
     Konstante statt einer mit dem Vertrieb abgestimmten Konfigurationsoption) oder
     `wohnform == "UNKLAR"` - direkte Umsetzung von CLAUDE.mds "Ein Fehlalarm ist für den
     Vertrieb billiger als ein verpasstes Projekt": unsichere Treffer sollen im Dashboard
     auffallen statt zwischen sicheren unterzugehen. Sichtbar an drei Stellen: eigene Spalte in
     der editierbaren Projekttabelle, ein `st.warning()` mit Trefferzahl direkt unter der
     Filterzeile, dezenter Warn-Hintergrund (`_row_highlight()`, per `Styler.apply()`) auf der
     ganzen Zeile in "Alle Felder", sowie ein Präfix vor dem Projektnamen im
     Vorkommen-Auswahlfeld. `pruefen` auch in `EXPORT_SPALTEN`, damit der Vertrieb in
     CSV/Excel danach filtern/sortieren kann.
   - Neue `database.list_status_log_fuer_projekt()` speist einen Aufklapp-Bereich
     "Statusverlauf (N)" (`st.expander`) unter der bestehenden Vorkommen-Ansicht in der
     Projekt-Detailauswahl - zeigt Zeitpunkt/alten/neuen Status/Notiz aus der schon vorhandenen,
     bisher im Dashboard ungenutzten `status_log`-Tabelle.
   - **Dabei gefundener, unabhängig von diesen zwei Features bestehender Bug in
     `radar/demo_data.py`:** `demo_db_path()` löste den Pfad zur Demo-Datenbank fest gegen
     `PROJECT_ROOT` auf (`settings.resolve_path("data/demo/demo.db")`), unabhängig von der
     übergebenen `settings.database.path` - Tests mit einer `tmp_path`-Settings-Instanz liefen
     dadurch unbemerkt gegen die ECHTE `data/demo/demo.db` im Projektverzeichnis statt gegen eine
     isolierte Kopie. Sichtbar geworden über den neuen Statusverlauf: ein Demo-Projekt zeigte 47
     status_log-Einträge statt 1, angehäuft über viele `pytest`-Läufe dieser Session. Jetzt löst
     `demo_db_path()` relativ zu `settings.database.path`s Verzeichnis auf (`demo/`-Unterordner
     daneben) - für die echte Konfiguration unverändert (`data/demo/demo.db`), für Tests mit
     `tmp_path` jetzt tatsächlich isoliert. Zweite, verwandte Ursache in `demo_data.seed()`
     behoben: das abschließende `set_projekt_status()` lief bisher bei jedem `seed()`-Aufruf
     unbedingt, auch wenn sich der Status gar nicht änderte, und hätte bei jedem manuellen Reset
     (`scripts/seed_demo_data.py`) weitere "Neu -> Neu"-Einträge angehäuft - jetzt nur noch bei
     tatsächlicher Änderung. Die dadurch verunreinigte echte `data/demo/demo.db` gelöscht (wird
     beim nächsten Aktivieren des Demo-Schalters sauber neu angelegt). Manuell im Browser mit
     Demo-Daten verifiziert (Warn-Badge auf Berlin-Pankow und Bad Oldesloe, Zeilen-Highlight in
     "Alle Felder", Statusverlauf-Expander zeigt nach dem Fix wieder genau 1 Eintrag). 11 neue
     Tests (davon 2 Regressionstests für den Pfad-/status_log-Bug), 240 insgesamt.

   **Nachträglich ergänzt: Diagramme, PDF-Report, Demo-Reset-Button.**
   - **Diagramme:** neuer Tab "Diagramme" (neben "Projekte"/"Quellen-Monitoring") mit zwei
     interaktiven Altair-Balkendiagrammen über den *gesamten* aktuell geladenen Datensatz (wie
     die KPI-Kacheln: unabhängig von den Zeilenfiltern der Projekttabelle) - "Projekte nach
     Status" (Reihenfolge folgt `settings.dashboard.statuses`, ein im Dashboard frei
     eingetragener unbekannter Status hängt hinten an statt zu verschwinden) und "Geplante
     Wohneinheiten nach Region" (Summe `we_gesamt` je Bundesland-Kürzel, absteigend sortiert).
     Pure Datenaufbereitung (`projekte_je_status()`, `we_je_region()`) von den
     Altair-Chart-Buildern (`chart_projekte_je_status()`, `chart_we_je_region()`) getrennt, beide
     Ebenen einzeln testbar. Farbe: Vega-Scheme "oranges" (markenkonsistent, sequentiell
     hell->dunkel), bewusst ohne Legende - die x-Achsenbeschriftung benennt die Kategorie schon
     eindeutig, eine zusätzliche Legende wäre redundante Kodierung. `altair` war über Streamlit
     bereits eine transitive Abhängigkeit (native `st.*_chart`-Elemente bauen darauf auf) - keine
     neue Abhängigkeit nötig.
   - **PDF-Report:** dritter Download-Button "PDF-Report herunterladen" neben CSV/Excel, über
     `to_pdf_bytes()` mit `fpdf2` (MIT-Lizenz, reines Python, keine Systemabhängigkeit wie
     Cairo/Pango bei WeasyPrint - passt zum schlanken `python:3.12-slim`-Image, siehe
     requirements.txt). Kompakte Spaltenauswahl (`PDF_SPALTEN`, eigene, kleinere Auswahl als
     `EXPORT_SPALTEN` - ein PDF ist zum Überfliegen gedacht) im A4-Querformat mit Kopf-/Fußzeile
     (Seitenzahl). **Zwei Encoding-Fallstricke gefunden:** (1) fpdf2s Kernschriften (Helvetica,
     keine eingebettete Unicode-Schrift) kennen nur Latin-1 - ein einzelnes Sonderzeichen in
     einem gescrapten Projektnamen (z. B. ein Emoji) hätte den ganzen Export mit einer
     `FPDFUnicodeEncodingException` abgebrochen; `_pdf_safe()` ersetzt unbekannte Zeichen jetzt
     defensiv statt zu crashen. (2) Deutsche Umlaute (ä/ö/ü/ß) werden mit der Kernschrift visuell
     korrekt gerendert (manuell im PDF-Viewer/Browser geprüft, siehe Screenshot-Verifikation),
     aber pdfplumber (das eigene PDF-Extraktions-Werkzeug des Projekts, siehe `radar/parser.py`)
     liefert für sie beim Zurück-Extrahieren keine verlässlichen Zeichen, weil fpdf2 hier keine
     ToUnicode-CMap einbettet - deshalb prüfen die Tests für Umlaut-Inhalte nur auf visuelle
     Erzeugung (PDF-Magic-Bytes, keine Exception), inhaltliche Extraktions-Tests verwenden bewusst
     nur Latin-1-unkritische Werte (Ortsnamen ohne Umlaut, Zahlen).
   - **Demo-Reset-Button:** "Demo-Daten zurücksetzen" direkt unter dem Demo-Schalter (nur
     sichtbar, wenn Demo-Modus aktiv). Neue `demo_data.reset_demo()` - anders als
     `ensure_demo_seeded()` (befüllt nur beim allerersten Mal) läuft `seed()` hier *unbedingt*,
     auch bei bereits gefüllter Demo-DB, und setzt zusätzlich `notizen` zurück (die `seed()`
     selbst nicht anfasst) - ein expliziter Reset ist bewusst die eine Ausnahme von der sonst
     projektweiten Regel "manuell gepflegte Felder nie überschreiben" (die gilt für echte Läufe
     gegen `data/radar.db`, nicht für den Demo-Sandkasten).
   - Manuell im Browser verifiziert: beide Diagramme mit Tooltip-Interaktion, heruntergeladener
     PDF-Report optisch geprüft (Screenshot der gerenderten Seite), Reset-Button macht eine im
     Demo-Modus gesetzte Notiz nachweislich rückgängig. 17 neue Tests, 257 insgesamt.

   **Nachträglich ergänzt: größeres Sidebar-Logo.** `st.logo(..., size="large")` rendert nur
   32px hoch - Streamlits größte eingebaute Stufe reichte nicht. `HUMMEL_CSS` überschreibt jetzt
   `img[data-testid="stSidebarLogo"]` auf 56px (mit `!important`, da die Höhe sonst aus einer
   generierten Streamlit-CSS-Klasse kommt, nicht aus einem Inline-Style) und verkleinert Bild-
   Margin sowie `stSidebarHeader`s `margin-bottom`, damit trotz des größeren Logos nicht mehr
   Leerraum als vorher entsteht. `stSidebarLogo`/`stSidebarHeader` sind keine offiziell
   dokumentierten testids, sondern am 21.09.2026 live im DOM verifiziert (siehe Kommentar im
   CSS) - können sich mit einem künftigen Streamlit-Update ändern. Manuell im Browser geprüft
   (Zoom-Screenshot der Sidebar, kein Überlappen mit "Demo-Modus" darunter). 3 neue Tests, 260
   insgesamt.
6. 🟡 Weitere Adapter.
   - `radar/sources/oparl.py` fertig implementiert und gegen die echte, öffentliche OParl-1.1-API
     der Gemeinde Uplengen (Niedersachsen, STERNBERG SD.NET RIM) live verifiziert:
     System/Body/Meeting-Liste (paginiert, nicht chronologisch sortiert), Meeting-Detail mit
     Tagesordnung, Consultation und Paper (Vorlage inkl. Anlagen), Gruppierung mehrerer Dokumente
     unter einer Vorlage über `paper.id`, Sonderfall Tagesordnungspunkt ohne Paper. Details und der
     dabei gefundene Bug in der robots.txt-Prüfung (`urllib.robotparser` -> `protego`, betrifft
     alle Adapter) stehen unter "Referenzquelle Uplengen/OParl". `config/sources.yaml` enthält
     Uplengen mit `status: verified`, aber `enabled: false` (robots.txt sperrt dort PDF-Downloads,
     die Quelle wäre produktiv wertlos, siehe dort). **Noch offen:** eine echte, nutzbare
     OParl-Quelle im Zielgebiet ohne PDF-Sperre finden und aktivieren.
   - ALLRIS direkt scheiterte an robots.txt (alle 7 Hamburger Bezirke sperren pauschal) - deshalb
     zwei Ersatzquellen: `radar/sources/hamburg_transparenz.py` (offizielle CKAN-API,
     Bebauungspläne, aber nur Satzungsbeschluss-Stufe) und `radar/sources/bv_hh.py` (inoffizielles
     Community-Portal, deckt zusätzlich frühe Verfahrensstufen ab, liefert aber nur Haupttext ohne
     Anlagen). Details unter "Referenzquelle Hamburg". Dabei zwei Erweiterungen an gemeinsamem
     Code (nicht nur neue Adapter-Dateien): `Source.additional_hosts`/`ignore_robots_txt`/
     `request_delay_seconds` (`radar/config.py`, `radar/http_client.py`) und `Dokument.text`/
     `Textinhalt` als Alternative zu einem PDF-Pfad (`radar/sources/base.py`, `radar/parser.py`,
     `radar/scraper.py`). Beide neuen Quellen `status: verified`, aber `enabled: false` - vor der
     Aktivierung mit dem Vertrieb abstimmen.
   - 171 Tests insgesamt (siehe "Referenzquelle Hamburg" für die Aufschlüsselung der neuen).
   - ALLRIS im Hamburger Umland (Tier A, Kreis Pinneberg/Segeberg/Stormarn) geprüft: alle
     robots.txt-offenen Kandidaten (Wedel, Amt Siek, Amt Hohe Elbgeest, Amt Pinnau) laufen auf
     "ALLRIS net" 4.x (Apache Wicket, komplett AJAX-/session-basiert) statt dem klassischen,
     GET-basierten ALLRIS - mit reinem HTTP+BeautifulSoup nicht sinnvoll scrapebar. Bewusst gegen
     einen Headless-Browser entschieden (Architektur bleibt leichtgewichtig). Details unter
     "Referenzquelle ALLRIS/Hamburger Umland". `radar/sources/allris.py` bleibt Stub,
     `config/sources.yaml` enthält "wedel" mit dem neuen `status: blocked` zur Dokumentation.
   - **Noch offen:** übrige Tier-A-Quellen im Hamburger Umland (Ahrensburg, Pinneberg, Bad
     Oldesloe, Reinbek - noch keine nutzbare Quelle gefunden) und der weiteren Startliste anbinden.
7. ✅ Dockerfile, Scheduler, CLI-Einstiegspunkt, Übergabedokumentation für die IT.
   CLI (`radar/cli.py`, `python -m radar <scrape|scheduler>`) und der Cron-freie Python-Scheduler
   (`radar/scheduler.py`, Begründung im Modul-Docstring: kein zusätzliches Paket, keine
   Cron-Rechte für den Nicht-Root-Nutzer nötig, ohne Netzzugriff testbar) waren bereits vor
   diesem Abschnitt implementiert und getestet (`tests/test_cli.py`, `tests/test_scheduler.py`).
   Neu: `Dockerfile` (python:3.12-slim, Nicht-Root-Nutzer `radar`, `TZ=Europe/Berlin` gesetzt -
   `scheduler.hour` in `config/settings.yaml` ist als deutsche Wanduhrzeit gemeint, gepinnte
   Abhängigkeiten aus `requirements.lock.txt`, Healthcheck gegen Streamlits
   `/_stcore/health`) und `docker-compose.yml` mit zwei Diensten aus demselben Image: `dashboard`
   (Streamlit, Port 8501, Healthcheck aktiv) und `scheduler` (`python -m radar scheduler`,
   Healthcheck deaktiviert - kein HTTP-Endpunkt). Beide teilen sich `./data` als Volume
   (Datenbank, PDF-/LLM-Cache, Lauf-Logs aus dem Dashboard-Subprozess). `scripts/run_daily.sh`
   löst über `docker compose run --rm scheduler python -m radar scrape` einen Lauf sofort aus,
   ohne auf den nächsten planmäßigen Zeitpunkt zu warten - für Tests nach Konfigurationsänderungen
   oder als host-seitige Cron-Alternative, falls die Firmen-IT das bevorzugt (dann den
   `scheduler`-Dienst nicht mitstarten, um Doppelläufe zu vermeiden).
   **Nachträglich behoben:** `radar/database.py` setzte ursprünglich kein `PRAGMA busy_timeout`
   (Python-`sqlite3`-Standard: 0, kein Warten). Liefen `dashboard`-Subprozess (manueller
   Knopfdruck) und `scheduler`-Dienst zufällig gleichzeitig, konnte ein `database is
   locked`-Fehler auftreten trotz `journal_mode=WAL`, da WAL nur gleichzeitiges Lesen+Schreiben
   erlaubt, nicht zwei gleichzeitige Schreiber. `connect()` setzt jetzt `PRAGMA busy_timeout =
   5000` (5 s Wartezeit statt sofortigem Fehler), getestet in
   `tests/test_database.py::test_connect_sets_busy_timeout`.
   Übergabedokumentation für die IT (`docs/IT_COMPLIANCE.md`): Anbieter (Anthropic, PBC),
   verarbeitete Datenkategorien (ausschließlich öffentliche Kommunaldokumente, keine
   automatisierte Anonymisierung – Einzelfälle mit privaten Antragstellern bewusst nicht
   wegdefiniert, siehe CLAUDE.md-Regel "auf das Nötige beschränken"), Datenfluss/Speicherort
   (Download+PDF-Text lokal im Container, nur vorgefilterte/relevante Seiten per HTTPS an die
   Claude API, Ergebnis-JSON in `data/radar.db`) und API-Key-Verwaltung (nur `ANTHROPIC_API_KEY`
   über `.env`/`env_file`, nie im Image). Bewusst **nicht** abschließend geklärt: AVV/DPA,
   internationale Datenübermittlung in die USA, aktuelle Aufbewahrungsfristen bei Anthropic –
   an Rechtsabteilung/Datenschutzbeauftragten verwiesen, das ist keine Code-Entscheidung.

   **Nachträglich ergänzt: E-Mail-Benachrichtigung bei neuen Treffern** (`radar/notifier.py`,
   CLAUDE.md-Roadmap-Vorgabe "optional E-Mail bei neuen Treffern"). SMTP-Konfiguration
   ausschließlich über `.env` (`SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
   `NOTIFICATION_RECIPIENT`) - fehlt eine Variable, bleibt die Benachrichtigung bewusst still
   deaktiviert (`load_smtp_config()` liefert `None`), kein Fehler, kein abgebrochener Lauf: ein
   Versandfehler darf einen sonst erfolgreichen Scraper-Lauf nicht stoppen (`send_notification()`
   fängt SMTP-Fehler ab, loggt sie, gibt `False` zurück). HTML-Mail mit Text-Alternative
   (Kommune, Vorhaben, WE-Zahl, Link zur Sitzung/Quelle je Zeile).
   - **Nur echte Neuanlagen werden gemeldet**, keine bloße Aktualisierung eines schon bekannten
     Projekts (z. B. wenn dieselbe Adresse in einer weiteren Sitzung erneut auftaucht, CLAUDE.md:
     "ein Projekt taucht in mehreren Gremien und Sitzungen auf"). Dafür mussten
     `database.upsert_projekt()`/`speichere_analyse_ergebnis()` zusätzlich `ist_neu: bool`
     zurückgeben (Muster wie `upsert_vorlage()`s `(id, braucht_analyse)`) - alle bestehenden
     Aufrufer/Tests entsprechend angepasst (Rückgabe ist jetzt ein Tupel statt eines nackten
     `int`).
   - `radar/scraper.py`: `SourceRunResult.neue_treffer: list[NeuerTreffer]` sammelt diese
     Neuanlagen pro Quelle während `_collect_and_persist()`. `radar/scheduler.py` sammelt sie
     über alle Quellen eines Laufs ein und ruft `send_notification()` genau einmal pro Lauf auf
     (eine Sammel-Mail, keine E-Mail pro Quelle).
   - CLI-Testbefehl `python -m radar test-email` (`radar/cli.py`): sendet zwei Beispiel-Treffer
     unabhängig von einem echten Scraper-Lauf, meldet fehlende SMTP-Konfiguration im Gegensatz
     zum stillen Scheduler-Verhalten hier ausdrücklich auf der Konsole (Exit-Code 1).
   - Nimmt STARTTLS auf dem konfigurierten Port an (Normalfall Port 587). Ein SMTP-Server mit
     implizitem TLS (Port 465) wurde nicht verifiziert - noch kein echter Versandtest gegen einen
     realen Mailserver, nur gegen ein Fake-SMTP-Objekt in `tests/test_notifier.py`.

   **Kritische Selbstprüfung nach der Einbindung (busy_timeout, CSV-Export, Notifier) ergab vier
   feine Kanten, alle behoben:**
   - `radar/scraper.py`: `_, ist_neu = projekt_ergebnis` entpackte blind ein Tupel, dessen
     Nicht-`None`-Sein nur implizit über zwei getrennte Funktionen (`AnalyseStatus.RELEVANT`
     impliziert `extraktion` gesetzt) garantiert war, ohne dass der Typ das erzwingt - ein
     stiller `TypeError` hätte den Rest der Quelle in diesem Lauf abgebrochen. Jetzt ein
     expliziter `None`-Check mit `logger.error` als Sicherheitsnetz (CLAUDE.md: "Fail loud").
   - `radar/notifier.py`: `SMTP_PORT=` (Variable gesetzt, aber leer - z. B. versehentlich beim
     Ausfüllen von `.env` geleert) führte zu `int("")` -> die Benachrichtigung wurde still
     deaktiviert statt des dokumentierten Defaults 587. Ein leerer Wert wird jetzt wie ein
     fehlender behandelt.
   - `app.py`: Das "Vorkommen"-Auswahlfeld rief `st.selectbox` mit einer leeren Optionsliste auf,
     sobald die Sidebar-Filter ein Projekt komplett herausfilterten - jetzt ein Hinweistext statt
     eines leeren Widgets. Daneben zeigte das Label für Projekte ohne extrahierte Bezeichnung
     (CLAUDE.md: "unklare Fälle nie stillschweigend verwerfen", kommt also real vor) wörtlich
     "None (Ort)" an - jetzt "(ohne Bezeichnung)".
   - `app.py`: `build_projekte_df()` ließ pandas WE-Zahl-Spalten (`we_gesamt`, `we_miete`,
     `we_gefoerdert_anzahl`, `seite`) automatisch auf `float64` hochkonvertieren, sobald
     mindestens ein Projekt einen fehlenden Wert hatte (Normalfall bei "prüfen"-markierten
     Projekten) - sichtbar als "18.0" statt "18" sowohl in der Dashboard-Tabelle als auch im
     CSV-Export. Jetzt `astype("Int64")` (pandas' nullable Integer) für diese Spalten.
   Alle vier mit Regressionstests abgesichert (`tests/test_scraper.py`, `tests/test_notifier.py`,
   `tests/test_app.py`).

   **Danach drei gezielt nachgefragte Erweiterungen umgesetzt:**
   - **E-Mail bei manuellen Läufen, aber standardmäßig aus** (Spam-Vermeidung bei Testläufen):
     `python -m radar scrape` bekam das optionale Flag `--notify` (`radar/cli.py`) - ohne das
     Flag wird `send_notification()` gar nicht erst aufgerufen, unabhängig davon, ob SMTP
     konfiguriert ist oder neue Treffer da sind. `app.py`s "Lauf jetzt starten"-Button hat dafür
     eine Sidebar-Checkbox "E-Mail-Benachrichtigung senden" (Default: aus) bekommen; der
     Dashboard-Subprozess läuft jetzt über `python -m radar scrape` (statt direkt
     `python -m radar.scraper`), damit dasselbe `--notify` greift - eine kleine, testbare
     `_scrape_command(notify: bool)`-Hilfsfunktion baut die Kommandozeile. Der `scheduler`-Dienst
     bleibt unverändert: dort wird immer benachrichtigt, kein Flag nötig.
   - **Mehrere Empfänger:** `NOTIFICATION_RECIPIENT` akzeptiert jetzt eine oder mehrere,
     kommagetrennte Adressen. `SMTPConfig.recipient: str` wurde zu `recipients: tuple[str, ...]`
     (Breaking Change für alle Aufrufer/Tests, entsprechend angepasst) - wichtig dabei:
     `smtplib.sendmail()` erwartet eine Liste *einzelner* Adressen als Envelope-Empfänger, eine
     einzelne kommagetrennte Zeichenkette als ein Listenelement wäre beim Versand fehlgeschlagen.
     Der `To`-Header bekommt die Adressen dagegen als eine kommagetrennte Zeichenkette (RFC 5322).
   - **`docs/IT_COMPLIANCE.md`** um einen Abschnitt "Zweiter externer Datenfluss:
     E-Mail-Benachrichtigung (SMTP)" ergänzt: der SMTP-Relay ist kein fester Anbieter, sondern
     von der Firmen-IT selbst in `.env` gewählt; übertragen wird nur eine kurze Zusammenfassung
     (Kommune, Vorhaben-Titel, WE-Zahl, Quell-Link) je Treffer, kein Dokumenttext, keine Anlagen.

## Arbeitsweise
- Nach jedem Schritt anhalten, Ergebnis zusammenfassen, offene Fragen stellen.
- Vor Schritt 3 nach dem `ANTHROPIC_API_KEY` fragen; vor dem ersten Live-Abruf `SCRAPER_CONTACT` klären.
- Fertig heißt: Tests grün, `ruff check` sauber, keine Secrets im Repo, README aktuell.
