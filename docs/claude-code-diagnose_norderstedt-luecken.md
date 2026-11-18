# Auftrag für Claude Code: Warum fehlen diese Norderstedt-Projekte im Radar?

> Im Terminal im Ordner `bautraeger-radar` starten (`claude`) und alles ab „AUFTRAG" einfügen.

---

## AUFTRAG

Hummel Küchen kennt die folgenden Norderstedter Projekte, das Radar zeigt sie nicht. Finde für **jedes einzelne** heraus, an welcher Stelle der Pipeline es verloren geht. **Noch nichts am Code ändern**, erst diagnostizieren und berichten.

### Schritt 1 – Umfang des Scrapers klären
- Welche Gremien werden im Ratsinfo Norderstedt (SessionNet, `buergerinfo.norderstedt.de`) gelesen? Nur ASV oder auch Hauptausschuss und Stadtvertretung?
- Welcher Zeitraum (ab welchem Sitzungsdatum)?
- Welche Dokumenttypen (Tagesordnung, Vorlagen, Anlagen-PDFs, Niederschriften)?

### Schritt 2 – Pro Projekt prüfen
Suche in allen gespeicherten Rohdaten (Dokument-Tabelle, gecachte PDFs/Texte, Log) **und** in der Projekt-Tabelle nach den Suchbegriffen. Ordne jedes Projekt genau einer Ursache zu:

- **A) Nie geladen** – kein Dokument mit den Suchbegriffen vorhanden (Quelle/Gremium/Zeitraum fehlt)
- **B) Geladen, aber nicht als Projekt erkannt** – Text vorhanden, LLM/Parser hat kein Projekt daraus gemacht
- **C) Erkannt, aber weggefiltert** – z. B. WE-Schwelle, Eigentum-Filter, fehlende WE-Zahl, Phase, Dublettenlogik
- **D) Vorhanden** – steht in der DB, wird aber im Dashboard nicht angezeigt (Anzeige-Filter)

| Nr. Hummel | Projekt | Suchbegriffe |
|---|---|---|
| 4062 | 7 Eichen | „7 Eichen“, „Sieben Eichen“, „Glashütter Damm“ |
| 4887 | Ohepark / B-Plan 322 | „322“, „An der Ohechaussee“, „Robert-Schumann-Straße“, „Ohepark“ |
| 5114 | Harkshörner Weg | „Harkshörner Weg“, „Rahmenplan“ |
| 5174 | Hermann-Klingenberg-Ring / Lawaetzstraße | „Klingenberg“, „Lawaetz“, „Norderstedter Modell“ |
| 5298 | Segeberger Chaussee 121–125a | „Segeberger Chaussee 121“, „125“, „REWE“, „ZooRoyal“ |
| 5320 | Bahnhofstraße / Schweriner Straße | „Schweriner Str“, „Bahnhofstr“, „Adlershorst“ |
| 5401 | Falkenkamp 4 | „Falkenkamp“ |
| 5494 | Königsberger Straße | „Königsberger“ |
| 5556 | Liesel-Hünichen-Weg | „Hünichen“ |
| 5571 | Postgelände Falkenbergstraße 17 | „Falkenbergstr“, „Post“ |
| – | Garstedter Tor / B-Plan 337 (Plambeck/GARTO) | „337“, „Garstedter Tor“, „GARTO“, „Kohfurth“, „Stettiner Straße“ |

Hinweis zu Garstedter Tor: Ich hatte es am 30.10. fälschlich mit Nordlicht gleichgesetzt und ausgeschlossen. Es ist ein eigenes Projekt (Plambeck bzw. GARTO GmbH & Co. KG, ca. 250 WE, 30 % gefördert). Prüfe auch, ob es durch meine Ausschlussregel herausgefallen ist.

### Schritt 3 – Bericht
Gib mir eine Tabelle: Projekt | Ursache A/B/C/D | Beleg (Datei/Zeile/Dokument-ID) | konkreter Fix-Vorschlag. Danach eine priorisierte Liste der Fixes (größter Recall-Gewinn zuerst).

### Schritt 4 – erst nach meinem OK
Lege `tests/fixtures/known_projects_norderstedt.json` mit allen von Hummel bekannten Projekten an und einen Test, der den Recall gegen diese Liste misst (Treffer über Adresse/Straßenname). Der Test darf vorerst als `xfail` markiert sein, solange die Fixes fehlen. `pytest` und `ruff check .` müssen danach sauber sein.
