# Handauswertung der wartenden Unterlagen (Stand 24.11.2026)

Solange die KI-Auswertung ausgeschaltet ist (`betrieb.live_api_aufrufe: false`, kein Guthaben), hat Claude die
451 wartenden Unterlagen aus `data/radar.db` (Status `BUDGET_ERSCHOEPFT`) im Chat von Hand ausgewertet. Die
Ergebnisse stehen als **„✍️ manuell nachgetragen“** in der Demo-Datenbank, **nicht** in `data/radar.db`. Am Wert
„Radar findet X von 18“ ändern sie deshalb nichts. Für den späteren KI-Pilot sind sie die Vergleichsliste.

## Vorgehen

1. **Zusammenfassen:** 451 Unterlagen sind 367 verschiedene Vorlagen (eine Vorlage läuft durch mehrere Gremien).
2. **Grob sortieren nach Betreff:** rund 300 sind kein Wohnbau (Haushalt, Satzungen, Verkehr, Berichte, Gewerbe,
   Polizeimeldungen). Rund 35 Wohnbau-Vorhaben blieben übrig, 14 davon standen schon als Projekt im Dashboard.
   Die Zuordnung steht in `data/handauswertung/arbeit/triage.json` (nicht eingecheckt).
3. **Genau lesen:** die 22 übrigen Vorhaben (Seiten mit Wohnbau-Begriffen aus allen zugehörigen Vorlagen), nach
   den Regeln aus `CLAUDE.md` („Fachliche Regeln“), Zahlen nur aus dem Text.
4. **Prüfen:** `scripts/handauswertung_uebernehmen.py` übernimmt nur Einträge, deren Zitat wörtlich in der
   Unterlage steht (11 von 11 bestanden), und schreibt `data/handauswertung/projekte.json`. Die vier stärksten
   Treffer wurden zusätzlich von Hand am Text nachgelesen.

## Neue Projekte (11)

| Vorhaben | WE | Stand | Hinweis |
|---|---|---|---|
| B-Plan 8 Glashütte, 9. Änd. „Glashütter Markt“ | 112, alle gefördert (davon 38 „Junges Wohnen“) | Auslegung | B 26/0432 |
| Wohnpark Tarpenwiesen, Kreuzweg / Glashütter Damm (Semmelhaack) | ca. 78, davon 50 % gefördert, dazu Seniorenwohnanlage ca. 93 Zimmer | Grundsatzbeschluss | B 25/0333 |
| B-Plan 364 Friedrich-Ebert-Str. / Friedrichsgaber Weg (FLUWOG) | ca. 43, davon 50 % gefördert | Aufstellung | passt zur Pressemitteilung vom 13.11.2026 (Infoabend 25.11.) |
| B-Plan 352 Buckhörner Moor / Pappelstieg | ca. 100 (Varianten 2024) | Auslegung | Zahl im Entwurf 2026 nicht bestätigt – prüfen |
| Quartier Kösliner Weg (B-Plan 341) | 198 | Bauantrag | **= „Nordlicht“ (Instone)**, steht schon als Pressefund – nicht doppelt zählen |
| Mehrfamilienhaus südwestliche Wiesenstraße | „acht bis neun Wohnungen“ | Bauantrag | klein |
| Umrüstung Unterkunft Segeberger Chaussee zu geförderten Wohnungen | Einzelposten 10/2/6/1 WE | unklar | Gebäude steht, Küchenzeilen vorhanden |
| Fläche EuropCar (Ulzburger Str.) | offen | Vorberatung | Stellplatzschlüssel 0,7 je WE beschlossen |
| B-Plan 145, 1. Änd. (Segeberger Chaussee) | offen | Aufstellung | Mischgebiet wird Wohnbaufläche |
| B-Plan 325 Europaallee / Willy-Brandt-Park | offen | Auslegung | vor allem Polizeineubau – schwacher Hinweis |
| 19. FNP-Änderung „Östlich Meeschensee“ | offen | Aufstellung | sehr früh |

## Geprüft, nicht relevant (8)

B-Plan 349 (Bauhof), 316 B (Flüchtlingsunterkunft, gebaut), 347 (Schule/Kita), 110 22. Änd. (ruht), 344 (Bestand,
kein Vorhaben), 250/250 B (Ein- und Zweifamilienhäuser), 334 (nur Anlieferung Einzelhandel), 298 (Gewerbe),
163 2. Änd. (Tennishalle), UBS4 (71 geförderte WE, schon gebaut – nur Auszeichnung).

## Bereits als Projekt eingetragen (14, nicht erneut ausgewertet)

Garstedter Tor (337), 7 Eichen (353), Ohepark (322), Meyers Mühle (343), B-Plan 339, Grüne Heyde/SonnenHeyde,
B-Plan 319, Klingenberg-Ring, REWE Segeberger Chaussee, Glashütter Kirchenweg, Richtweg (305), Harkshörner Weg,
Postgelände (365), Liesel-Hünichen-Weg. Querabgleich: die MoBiLe-Mitteilung M 26/0419 nennt für B-Plan 322
„ca. 180 Wohneinheiten“ (Dashboard: 200 bzw. 193) – abgleichen.

## Grenzen

- Die 34 noa4-Artikel sind nur nach Titel sortiert: ihr Text liegt nicht in der Datenbank. Die passenden bewertet
  das Radar ohnehin selbst (KI-frei, siehe „Web-Funde“).
- Von großen Anlagen wurden nur die Seiten mit Wohnbau-Begriffen gelesen (höchstens 8 je Dokument).
- Die Auswertung ersetzt nicht den KI-Pilot: sie zeigt, was in den Unterlagen steht, nicht, ob das Radar es
  ohne Menschen findet.

## Wiederholen (neue Unterlagen)

Arbeitsdateien liegen unter `data/handauswertung/arbeit/` (Liste, Grobsortierung, Auszüge, `AUFTRAG.md` für die
Bewertung, Ergebnisse). Danach `.venv\Scripts\python.exe scripts\handauswertung_uebernehmen.py` und im Dashboard
„Demo-Daten zurücksetzen“ – oder die Demo-Datenbank übernimmt neue Einträge beim nächsten Start von selbst.
