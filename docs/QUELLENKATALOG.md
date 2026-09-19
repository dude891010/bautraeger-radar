# Quellenkatalog

Das Zielgebiet umfasst SH, HH, NI, HB, MV und selektiv Berlin. Das sind mehrere Tausend Gemeinden,
also wird **schrittweise nach Priorität** aufgebaut, nicht alles auf einmal. Wohnbauprojekte ab 6 WE
werden fast immer auf **Gemeinde-/Stadt-/Bezirksebene** beraten, nicht im Landkreis. In SH und MV
betreiben Ämter teils ein gemeinsames Ratsinformationssystem für mehrere Gemeinden.

## Neue Quelle aufnehmen (Checkliste)
1. Ratsinformationssystem der Kommune finden (Startseite → „Bürgerinfo", „Ratsinfo", „Sitzungsdienst").
2. **System bestimmen** (Fingerabdruck, siehe unten). Unklar → `system: unknown`, `status: candidate`.
3. Prüfen, ob es einen **OParl**-Zugang gibt. Falls ja, den bevorzugen (`system: oparl`).
4. `robots.txt` und Nutzungsbedingungen lesen, Ergebnis in `notes` festhalten.
5. Ausschussnamen der Kommune notieren, Regex in `committees:` eintragen, falls die Standardmuster nicht passen.
6. Eintrag in `config/sources.yaml`. Erst nach echtem Testabruf `status: verified`, dann `enabled: true`.
7. Antworten als Fixtures unter `tests/fixtures/<id>/` speichern.

## Fingerabdrücke der Systeme
| System | Erkennungsmerkmale |
|---|---|
| SessionNet (Somacos) | Meta-Tag `sessionnet`, Fußzeile „SessionNet Version …", Seiten `info.php`, `si0040.php`, `si0057.php`, `getfile.php` (Norderstedt, geprüft) |
| ALLRIS classic (CC e-gov) | Pfad `/bi/` mit `si010.asp`, `yw010.asp` (Muster aus Hamburg-Altona, ungeprüft - bei allen bisher geprüften erreichbaren Installationen tatsächlich schon durch "ALLRIS net" abgelöst, siehe unten) |
| ALLRIS net 4.x (CC e-gov) | Meta-Tag `ALLRIS net Version 4.x`, Seiten ohne `.asp`-Endung (`si010`, `to010`, `vo020`), Apache-Wicket-Ressourcen (`/wicket/resource/...`) - **komplett AJAX-/session-basiert, mit reinem HTTP+BeautifulSoup nicht sinnvoll scrapebar** (siehe unten, "blocked") |
| SD.NET RIM | OParl-Endpunkt laut Herstellerdoku unter `/webservice/oparl/v1.1/body` |
| OParl allgemein | JSON-`System`-Objekt an einer festen URL (Beispiel Münster: `oparl.stadt-muenster.de/system`) |

## Startliste (Vorschlag, mit dem Vertrieb abstimmen)
Systemtyp und Status sind überall offen, außer wo vermerkt. Tier ist ein Vorschlag für die Scan-Reihenfolge.

| Land | Gebiet | Tier | Stand |
|---|---|---|---|
| SH | **Norderstedt** | A | ✅ SessionNet, geprüft |
| SH | Hamburger Rand: Ahrensburg, Pinneberg, Elmshorn, Wedel, Reinbek, Geesthacht, Bad Oldesloe, Quickborn, Henstedt-Ulzburg, Schenefeld, Barsbüttel, Glinde | A | Wedel: 🚫 `blocked` (ALLRIS net 4.x, siehe unten). Elmshorn/Quickborn/Henstedt-Ulzburg/Schenefeld/Barsbüttel/Glinde: robots.txt sperrt alles. Geesthacht: Bot-Schutz (Captcha) vor ALLRIS. Reinbek: robots.txt nicht abrufbar (Verbindungsfehler, evtl. WAF). Ahrensburg/Pinneberg/Bad Oldesloe: noch keine offene Quelle gefunden |
| SH | Kiel, Lübeck, Flensburg, Neumünster | B | offen |
| HH | 7 Bezirksversammlungen: Altona, Bergedorf, Eimsbüttel, Hamburg-Mitte, Hamburg-Nord, Harburg, Wandsbek | A | ALLRIS direkt gesperrt (robots.txt aller 7 Bezirke); Ersatz: `hamburg_transparenz` (offiziell, nur Satzungsbeschluss) + `bv_hh_*` (inoffiziell, alle Stufen) ✅ beide verifiziert, `enabled: false` |
| NI | Hamburger Rand: Buchholz i. d. N., Winsen (Luhe), Seevetal, Stade, Buxtehude, Lüneburg, Neu Wulmstorf | A | offen |
| NI | Hannover, Braunschweig, Oldenburg, Osnabrück, Göttingen, Wolfsburg, Hildesheim | B | offen |
| HB | Stadt Bremen (Bau-/Stadtentwicklungsgremien, Beiräte), Bremerhaven | B | offen |
| MV | Rostock, Schwerin, Neubrandenburg, Stralsund, Greifswald, Wismar | B | offen |
| BE | 12 Bezirksverordnetenversammlungen | C (selektiv) | offen, Mindestgröße klären |

## OParl-Adapter: Erfahrungen aus der Referenzprüfung (Gemeinde Uplengen, 19.09.2026)
- Öffentliche OParl-Endpunkte lassen sich über die kuratierte Liste
  [`OParl/resources`](https://github.com/OParl/resources) (`endpoints.yml`) finden, sortiert nach Bundesland.
- **robots.txt und Lizenzbedingungen vor der Adapter-Verifikation prüfen, nicht danach:** von den geprüften
  Kandidaten im Zielgebiet (Amt Itzstedt/Amt Trave-Land in SH, Wallenhorst/Bad Pyrmont in NI) sperrten die
  meisten entweder generell (`Disallow: /`) oder verlangten eine vorherige Absprache laut Lizenzfeld im
  System-Objekt. Uplengen (NI) erlaubt die JSON-API vollständig, sperrt aber PDF-Downloads
  (`Disallow: /*.pdf$`) - dieses verbreitete Wildcard-Muster wird von `urllib.robotparser` nicht erkannt und
  hätte den Download fälschlich als erlaubt behandelt (siehe CLAUDE.md, jetzt per `protego` behoben).
  **Vor der Aktivierung einer neuen Quelle robots.txt gegen den tatsächlich benutzten HTTP-Client testen**,
  nicht nur überfliegen.
- Eine Quelle, deren robots.txt PDF-Downloads sperrt, ist für den echten Betrieb wertlos, wenn Vorlagen dort
  ausschließlich als PDF vorliegen (wie bei Uplengen) - trotzdem als `status: verified` mit `enabled: false`
  aufnehmen, wenn die Struktur echt geprüft wurde: das dokumentiert die Adapter-Verifikation, ohne die Quelle
  produktiv zu nutzen.
- Die Meeting-Liste eines OParl-Systems ist nicht notwendigerweise chronologisch sortiert und liefert nicht
  immer die volle Tagesordnung mit (insbesondere bei zukünftigen Sitzungen) - Adapter sollten alle Seiten
  lesen/selbst filtern und die Tagesordnung separat über die Meeting-Detail-URL abrufen.

## Hamburg: ALLRIS gesperrt, zwei Ersatzquellen (19.09.2026)
- Alle 7 Bezirks-ALLRIS-Seiten sperren Crawler pauschal (`Disallow: /`, offenbar eine zentrale
  Dataport-Hosting-Vorlage) - direktes Scraping scheidet aus.
- **Offiziell, aber unvollständig:** Transparenzportal Hamburg (`suche.transparenz.hamburg.de`,
  CKAN-API), Register `bauleitplaene` - gesetzlich vorgeschriebene Veröffentlichung, offene Lizenz,
  echte PDFs direkt gehostet. Erfasst aber nur bereits rechtskräftige Pläne (Satzungsbeschluss),
  nicht die frühen, für den Vertrieb wertvolleren Stufen. robots.txt sperrt `/api/` pauschal,
  widerspricht damit der von der Stadt selbst dokumentierten API-Nutzung - siehe CLAUDE.md,
  Abschnitt "Referenzquelle Hamburg" für die Abwägung. **Nicht** dasselbe wie „Bauleitplanung
  online" (`bauleitplanung.hamburg.de`) - das wurde nicht separat geprüft, könnte aber ähnliche
  oder ergänzende Daten haben, falls hamburg_transparenz/bv_hh nicht ausreichen.
- **Inoffiziell, aber vollständiger:** bv-hh.de (Community-Portal, github.com/bv-hh/bv-hh) spiegelt
  dieselben ALLRIS-Daten aller 7 Bezirke inkl. früher Verfahrensstufen, mit offener robots.txt.
  Liefert aber keine PDF-Anlagen, nur den extrahierten Haupttext je Drucksache - kein offizieller
  Betreiber, keine Verfügbarkeitsgarantie.
- Ausschussnamen variieren stark je Bezirk (Bergedorf z. B. „Stadtentwicklungsausschuss",
  „Fachausschuss für Bauangelegenheiten", „Unterausschuss für Bauangelegenheiten"). Deshalb
  Regex-Muster statt fester Namen; für `bv_hh_*` zusätzlich um die Bezirksversammlung selbst
  erweitert, siehe `config/sources.yaml`.

## ALLRIS im Hamburger Umland: "ALLRIS net" 4.x statt classic (19.09.2026)
- Von den geprüften, robots.txt-offenen Kandidaten (Wedel, Amt Siek, Amt Hohe Elbgeest, Amt Pinnau)
  lief **jeder einzelne** auf "ALLRIS net" Version 4.1.7 (Apache Wicket) statt dem im
  Fingerabdruck vermuteten klassischen, `.asp`-basierten ALLRIS. Diese Oberfläche ist komplett
  AJAX-/session-basiert: selbst der beim ersten Aufruf von `si010` angezeigte Kalendermonat wird
  nicht serverseitig mitgeliefert, sondern erst durch einen nachgelagerten, zustandsbehafteten
  AJAX-Aufruf befüllt (sitzungsgebundene Wicket-Komponenten-IDs, keine einfachen GET-Parameter).
  **Bewusste Architekturentscheidung: kein Headless-Browser** (CLAUDE.md, Scraping-Regeln bleiben
  bei BeautifulSoup + lxml) - ein Adapter dafür würde entweder das Wicket-AJAX-Protokoll nachbauen
  (aufwendig, fragil) oder einen Browser brauchen. Deshalb `status: blocked` (siehe `config/
  sources.yaml`, Quelle "wedel") statt eines Adapters. **Vor der nächsten ALLRIS-Suche**: erst
  prüfen, ob eine Installation wirklich noch die klassische `.asp`-Variante nutzt (Meta-Tag/
  Ressourcen-Pfade wie oben), bevor Zeit in die robots.txt-Prüfung investiert wird.
- Geesthacht (robots.txt offen) zusätzlich durch ein explizites Bot-Abwehr-Gate (ALTCHA-Captcha)
  vor ALLRIS geschützt - so etwas wird nie umgangen, unabhängig von robots.txt.

## Hinweise für weitere Regionen
- **Quoten und Begriffe** für geförderten Wohnraum sind regional verschieden (z. B. Hamburgs „Drittelmix",
  „Förderweg"; anderswo Sozialwohnungsquoten, „Belegungsbindung", „Wohnberechtigungsschein"). Die Stichwortliste
  in `settings.yaml` und der LLM-Prompt müssen diese Begriffe kennen; Mischprojekte sind hier die Regel.
- **Berlin:** Bezirksebene (BVV) plus Landesebene; zunächst nur Bezirke.
