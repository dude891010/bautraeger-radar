# Gesamtauftrag für Claude Code: Bauträger-Radar Norderstedt – Lücken schließen

> Im Terminal im Ordner `bautraeger-radar` starten (`claude`) und alles ab „AUFTRAG" einfügen. Diese Datei ersetzt die beiden früheren Aufträge (`claude-code-diagnose_norderstedt-luecken.md`, `claude-code-auftrag_norderstedt-demo-2.md`).

---

## AUFTRAG

### Hintergrund
Hummel Küchen hat seine eigene Projektliste für Norderstedt mit dem Radar abgeglichen. Ergebnis: Hummel kennt 11 Projekte, die das Radar nicht zeigt. Ziel: Das Radar soll diese Projekte **selbst** finden, nicht nur durch manuelles Nachtragen. Bisherige Erkenntnisse aus der laufenden Diagnose:
- Es gab nur **einen** Lauf, am 19.09.2026. Fenster 19.06.–19.11.2026.
- Gelesen wird **nur** der „Ausschuss für Stadtentwicklung und Verkehr“ (ASV), Muster exakt verankert.
- Die Sitzungen aus Oktober und November hatten beim Lauf noch keine Detailseite.
- Für Garstedter Tor (B-Plan 337) gibt es keinen Ausschluss im Code.

Allgemeine Regeln:
- `.env` nicht anfassen und nicht ausgeben.
- Keine Paywalls umgehen (Abendblatt), keine Seiten scrapen, deren robots.txt oder AGB das verbieten.
- Nach jeder Phase: `pytest` grün und `ruff check .` sauber.
- Kurzer Bericht, was geändert wurde.

---

### Phase 1 – Diagnose abschließen (kein Code ändern)
1. Den laufenden PDF-Volltext-Scan der 127 PDFs abschließen.
2. Für jedes Projekt unten genau eine Ursache zuordnen:
   - **A** nie geladen (Zeitfenster, Gremium, Dokumenttyp, Quelle fehlt)
   - **B** geladen, aber nicht als Projekt erkannt
   - **C** erkannt, aber weggefiltert (WE-Schwelle, Eigentum-Filter, fehlende WE, Phase, Dubletten)
   - **D** in der DB, aber im Dashboard nicht angezeigt
3. Zusätzlich prüfen, warum **Nordlicht** (Instone, Kösliner Weg 7–9, 198 Eigentumswohnungen) im Dashboard fehlt. Vermutet wird der Eigentum-Filter, das wäre gewollt.
4. Falls schon Einträge mit `herkunft = manuell_nachgetragen` existieren: in der Diagnose ignorieren.

| Nr. Hummel | Projekt | Suchbegriffe |
|---|---|---|
| 4062 | 7 Eichen | „7 Eichen“, „Sieben Eichen“, „Glashütter Damm“ |
| 4887 | Ohepark / B-Plan 322 | „322“, „An der Ohechaussee“, „Robert-Schumann-Straße“, „Ohepark“ |
| 5114 | Harkshörner Weg | „Harkshörner Weg“, „Rahmenplan“ |
| 5174 | Hermann-Klingenberg-Ring / Lawaetzstraße | „Klingenberg“, „Lawaetz“, „Norderstedter Modell“ |
| 5298 | Segeberger Chaussee 121–125a | „Segeberger Chaussee 121“, „125“, „REWE“, „ZooRoyal“ |
| 5320 | Bahnhofstraße / Schweriner Straße | „Schweriner Str“, „Bahnhofstr“, „Adlershorst“ (ASV 18.06.2026) |
| 5401 | Falkenkamp 4 | „Falkenkamp“ |
| 5494 | Königsberger Straße | „Königsberger“ |
| 5556 | Liesel-Hünichen-Weg | „Hünichen“ |
| 5571 | Postgelände Falkenbergstraße 17 | „Falkenbergstr“, „Post“ |
| – | Garstedter Tor / B-Plan 337 | „337“, „Garstedter Tor“, „GARTO“, „Kohfurth“, „Stettiner Straße“ |

**Bericht:** Tabelle Projekt | Ursache | Beleg (Datei/Dokument-ID/Sitzung) | Fix-Vorschlag.

**→ STOPP. Zeig mir die Tabelle und warte auf mein OK, bevor du Phase 2 startest.**

---

### Phase 2 – Ratsinfo-Scraper reparieren
1. **Rückwirkend nachladen:** ab 01.01.2024 bis heute.
2. **Fenster mit Überlappung:** Jeder Lauf schaut mindestens 6 Wochen zurück, damit kein Randtag verloren geht (Beispiel 18.06. vs. 19.06.).
3. **Sitzungen ohne Detailseite** merken und bei jedem Lauf erneut prüfen, bis Vorlagen bzw. Niederschrift da sind.
4. **Weitere Gremien:** zusätzlich **Hauptausschuss** und **Stadtvertretung**. Gremien konfigurierbar machen (in `config/`), nicht hart im Code.
5. **Regelmäßiger Lauf:** wöchentlich. Lösung für lokal (Windows-Aufgabenplanung, Anleitung in README) und für Docker (Cron im Container oder eigener Scheduler-Service in `docker-compose.yml`). Letzten Lauf mit Zeitstempel im Dashboard anzeigen, mit Warnhinweis, wenn er älter als 14 Tage ist.
6. Ausschlussregeln prüfen: Garstedter Tor (B-Plan 337) ist **nicht** Nordlicht und darf nicht blockiert werden.

---

### Phase 3 – Recall-Test mit Hummels Liste
1. `tests/fixtures/known_projects_norderstedt.json` mit **allen** Projekten aus Hummels Liste anlegen (siehe unten), plus Garstedter Tor.
2. Einen Test schreiben, der den Recall misst: Treffer über Straßenname, Adresse oder B-Plan-Nummer, nur Einträge mit `herkunft = radar`.
3. Ergebnis als Zahl ausgeben („X von Y gefunden“) und die fehlenden auflisten.
4. Projekte, die nur über Presse auffindbar sind (Liesel-Hünichen-Weg, Falkenkamp, Königsberger Straße), vorerst als `xfail` markieren, bis Phase 5 umgesetzt ist.

---

### Phase 4 – Neu laufen lassen und vergleichen
1. Vollständigen Lauf mit den Änderungen aus Phase 2 starten.
2. Recall-Test ausführen.
3. Bericht: Welche der 11 findet das Radar jetzt **selbst**? Welche fehlen weiter, und warum?

---

### Phase 5 – Manuell recherchierte Projekte als Demo-Daten (nach Phase 4)
Nur die Projekte eintragen, die das Radar in Phase 4 **nicht** selbst gefunden hat, mit Kennzeichnung:
- Feld `herkunft` (falls nicht vorhanden: Migration, Default `radar` für alle bestehenden Einträge).
- Neue Einträge bekommen `manuell_nachgetragen`. Im Dashboard als Badge „manuell nachgetragen“ anzeigen.
- Seed idempotent, keine Duplikate. Nordlicht, SonnenHeyde und Richtweg nicht anfassen.
- Bei `wohneinheiten: null` „WE offen“ anzeigen, nichts erfinden.

```json
[
  {"name": "Garstedter Tor (B-Plan 337)", "bauherr": "GARTO GmbH & Co. KG (laut Stadtunterlagen); laut Presse Plambeck", "adresse": "zwischen Kohfurth und Stettiner Straße, 22850 Norderstedt", "ortsteil": "Garstedt", "wohneinheiten": 250, "we_hinweis": "ca. 250 WE, 30 % gefördert", "phase": "B-Plan", "phase_detail": "Entwurf öffentlich ausgelegt bis 22.10.2024", "stand_datum": "2024-09-26", "quellen": [{"titel": "Stadt Norderstedt: PM B-Plan 337", "url": "https://www.norderstedt.de/media/custom/3224_9358_1.PDF"}, {"titel": "Stadt Norderstedt: Anlage GARTO im B-Plan 337", "url": "https://norderstedt.de/media/custom/3224_9229_1.PDF"}]},
  {"name": "7 Eichen", "bauherr": "offen", "adresse": "zwischen Glashütter Damm und Schleswig-Holstein-Straße, 22851 Norderstedt", "ortsteil": "Glashütte", "wohneinheiten": 500, "we_hinweis": "zunächst rund 500 Wohnungen auf 17 ha (Hummel: 300 WE – abgleichen)", "phase": "in Planung", "phase_detail": "Vorentwurf vorgestellt; B-Plan-Verfahren läuft", "stand_datum": "2026-11-13", "quellen": [{"titel": "noa4: 7 Eichen – Anwohner fürchten den Verkehrskollaps", "url": "https://www.noa4.de/7-eichen-anwohner-fuerchten-den-verkehrskollaps/"}]},
  {"name": "Ohepark – B-Plan 322 „An der Ohechaussee“", "bauherr": "Projektentwickler (nicht genannt)", "adresse": "Ohechaussee / Robert-Schumann-Straße, 22848 Norderstedt", "ortsteil": "Garstedt", "wohneinheiten": 200, "we_hinweis": "etwa 200 WE in neun Gebäuden, mind. 50 % gefördert", "phase": "B-Plan", "phase_detail": "Entwurf vorgestellt 05.11.2024; FNP-Änderung Stand 29.01.2026", "stand_datum": "2026-01-29", "quellen": [{"titel": "Stadt Norderstedt: PM B-Plan 322", "url": "https://www.norderstedt.de/media/custom/3224_9563_1.PDF"}, {"titel": "Stadt Norderstedt: 18. FNP-Änderung", "url": "https://www.norderstedt.de/media/custom/3224_13821_1.PDF"}]},
  {"name": "Rahmenplan Harkshörner Weg", "bauherr": "offen", "adresse": "Harkshörner Weg / Ulzburger Straße, 22844 Norderstedt", "ortsteil": "Harksheide", "wohneinheiten": 500, "we_hinweis": "rund 500 WE", "phase": "in Planung", "phase_detail": "Rahmenplan; Übergang in B-Plan", "stand_datum": "2025-04-03", "quellen": [{"titel": "Stadt Norderstedt: PM Rahmenplan Harkshörner Weg", "url": "https://www.norderstedt.de/media/custom/3224_10531_1.PDF"}]},
  {"name": "Segeberger Chaussee 121–125 (REWE + Wohnen)", "bauherr": "Investor (nicht genannt)", "adresse": "Segeberger Chaussee 121–125, 22850 Norderstedt", "ortsteil": "Harksheide", "wohneinheiten": 35, "we_hinweis": "35 WE über REWE-Markt (Hummel: 91 WE – abgleichen)", "phase": "in Planung", "phase_detail": "ASV-Entscheidung angesetzt", "stand_datum": "2025-11-20", "quellen": [{"titel": "noa4: Neues Quartier an der Segeberger Chaussee", "url": "https://www.noa4.de/neues-quartier-an-der-segeberger-chaussee-rewe-markt-mit-wohnungen/"}]},
  {"name": "Postgelände Falkenbergstraße", "bauherr": "ADLERSHORST Baugenossenschaft eG (laut Hummel)", "adresse": "Falkenbergstraße 17, 22844 Norderstedt", "ortsteil": "Harksheide", "wohneinheiten": 100, "we_hinweis": "rund 100 WE, mind. 50 % gefördert", "phase": "in Planung", "phase_detail": "ASV hat nächste Planungsrunde beschlossen", "stand_datum": "2026-09-21", "quellen": [{"titel": "noa4: 100 neue Wohnungen auf dem Postgelände", "url": "https://www.noa4.de/100-neue-wohnungen-auf-dem-postgelaende/"}]},
  {"name": "Hermann-Klingenberg-Ring („Norderstedter Modell“)", "bauherr": "Stadt Norderstedt", "adresse": "Hermann-Klingenberg-Ring / Lawaetzstraße, 22844 Norderstedt", "ortsteil": "Friedrichsgabe", "wohneinheiten": 40, "we_hinweis": "40 geförderte WE in 4 Gebäuden", "phase": "in Planung", "phase_detail": "ZURÜCKGESTELLT: Hauptausschuss empfiehlt Stopp wegen Haushaltslage", "stand_datum": "2026-09-29", "quellen": [{"titel": "noa4: Norderstedter Modell vorerst auf Eis gelegt", "url": "https://www.noa4.de/norderstedter-modell-vorerst-auf-eis-gelegt/"}]},
  {"name": "Liesel-Hünichen-Weg (ADLERSHORST)", "bauherr": "ADLERSHORST Baugenossenschaft eG", "adresse": "Liesel-Hünichen-Weg, 22844 Norderstedt", "ortsteil": "Harksheide", "wohneinheiten": 69, "we_hinweis": "69 geförderte WE, 2–4 Zimmer, Tiefgarage", "phase": "im Bau", "phase_detail": "Spatenstich erfolgt; Vermietung ab Q2 2027, Fertigstellung Q4 2027", "stand_datum": "2026-11-13", "quellen": [{"titel": "noa4: Spatenstich für 69 geförderte Wohnungen", "url": "https://www.noa4.de/spatenstich-fuer-69-gefoerderte-wohnungen-in-norderstedt/"}]}
]
```

Falkenkamp, Königsberger Straße und Bahnhofstraße/Schweriner Straße **nicht** eintragen. Dafür gibt es keine öffentlich prüfbare Quelle mit WE-Zahl.

---

### Phase 6 – Neue Quellen vorbereiten (nur Konzept, erst nach meinem OK umsetzen)
1. **noa4.de** (lokaler Sender, frei zugänglich): robots.txt und AGB prüfen, RSS oder Sitemap suchen, Konzept für einen Scraper der Rubrik Norderstedt mit LLM-Klassifizierung wie beim Ratsinfo.
2. **Pressemitteilungen der Stadt Norderstedt** (PDFs unter norderstedt.de): Fundort der Übersicht klären, gleiches Konzept.
3. Kurzer Plan mit Aufwand, danach STOPP.

---

### Fixture-Daten für Phase 3 (Hummels Liste, Stand 13.11.2026)

```json
[
  {"hummel_nr": null, "name": "Glashütter Kirchenweg", "suche": ["Glashütter Kirchenweg"]},
  {"hummel_nr": null, "name": "Ulzburger Str. 457-463 / Moorbektwiete (B-Plan 319)", "suche": ["Moorbektwiete", "319", "Ulzburger Straße 457"]},
  {"hummel_nr": 3933, "name": "Ohechaussee 20 / Meyers Mühle (B-Plan 343)", "suche": ["Ohechaussee 20", "343", "Meyers Mühle"]},
  {"hummel_nr": 5353, "name": "Berliner Allee / Schumanstraße (B-Plan 339)", "suche": ["Schumanstr", "339"]},
  {"hummel_nr": 5631, "name": "Berliner Allee 4-12", "suche": ["Berliner Allee 4"]},
  {"hummel_nr": 3893, "name": "Grüne Heyde / SonnenHeyde (Harckesheyde, Mühlenweg)", "suche": ["Grüne Heyde", "Sonnen Heyde", "SonnenHeyde", "Mühlenweg", "357"]},
  {"hummel_nr": 5403, "name": "Richtweg 28 / Garstedter Dreieck", "suche": ["Richtweg"]},
  {"hummel_nr": 4062, "name": "7 Eichen", "suche": ["7 Eichen", "Sieben Eichen", "Glashütter Damm"]},
  {"hummel_nr": 4887, "name": "Ohepark / B-Plan 322", "suche": ["322", "An der Ohechaussee", "Ohepark", "Ohechaussee 213"]},
  {"hummel_nr": 5114, "name": "Harkshörner Weg / Ulzburger Str. 490", "suche": ["Harkshörner Weg", "Ulzburger Straße 490"]},
  {"hummel_nr": 5174, "name": "Hermann-Klingenberg-Ring / Lawaetzstraße", "suche": ["Klingenberg", "Lawaetz"]},
  {"hummel_nr": 5298, "name": "Segeberger Chaussee 121-125a", "suche": ["Segeberger Chaussee 121", "Segeberger Chaussee 125"]},
  {"hummel_nr": 5320, "name": "Bahnhofstraße / Schweriner Straße", "suche": ["Schweriner Str"]},
  {"hummel_nr": 5401, "name": "Falkenkamp 4", "suche": ["Falkenkamp"], "nur_presse": true},
  {"hummel_nr": 5494, "name": "Königsberger Straße", "suche": ["Königsberger"], "nur_presse": true},
  {"hummel_nr": 5556, "name": "Liesel-Hünichen-Weg", "suche": ["Hünichen"], "nur_presse": true},
  {"hummel_nr": 5571, "name": "Postgelände Falkenbergstraße 17", "suche": ["Falkenbergstr", "Postgelände"]},
  {"hummel_nr": null, "name": "Garstedter Tor (B-Plan 337)", "suche": ["Garstedter Tor", "GARTO", "337"]}
]
```
