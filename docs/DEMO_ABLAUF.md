# Demo-Ablauf für den Chef (Stand 24.11.2026, Generalprobe 20.11.2026, Tabs seit 23.11. vereinfacht, Handauswertung 24.11.)

Dauer: ca. 15 Minuten. Ziel: zeigen, dass das Radar die richtigen Unterlagen **selbst** findet – und dass nur
noch die KI-Auswertung (kleines Guthaben) fehlt, um daraus automatisch Projekte zu machen.

## Vorher (5 Minuten)

1. Dashboard starten (PowerShell im Projektordner):
   `.venv\Scripts\python.exe -m radar app` – dann im Browser http://localhost:8501, Passwort aus `.env`.
2. Optional für frische Zahlen einen kurzen Lauf: `.venv\Scripts\python.exe -m radar scrape` (wenige Minuten,
   kostet nichts – die KI bleibt aus).
3. Zahlen für Schritt 5 bereitlegen: `.venv\Scripts\python.exe -m radar recall`.

## 1. Was das Radar liest (2 Min) – Tab „⚙️ Quellen“, Demo-Schalter **aus**

- Drei Quellen, alle ✅ OK: Ratsinformationssystem Norderstedt (Ausschuss Stadtentwicklung, Hauptausschuss,
  Stadtvertretung), die Pressemitteilungen der Stadt und die Lokalpresse noa4.de.
- „Erfasst ab 01.01.2024“: alles seit Anfang 2024 ist gelesen, jede Woche kommt Neues dazu.
- Kernsatz: **„Das Radar liest jede Woche automatisch, was im Rathaus und in der Lokalpresse zu Wohnbau steht.“**

## 2. Der erste echte Fund (2 Min) – Tab „🔥 Jetzt anrufen“, Demo-Schalter **aus**

- Unten „Baubeginne & Genehmigungen aus Presse und Web“: **Liesel-Hünichen-Weg, ADLERSHORST, 69 geförderte
  Wohnungen, Spatenstich – Vermietung ab Q2 2027.**
- Kernsatz: **„Das hat das Radar heute ganz allein gefunden, ohne KI – ein Projekt kurz vor dem Küchenbedarf.“**

## 3. So sieht es mit KI aus (5 Min) – Demo-Schalter **an**

- Oben der Hinweis: 26 echte, geprüfte Norderstedter Projekte (⭐) und 9 erfundene Beispiele aus anderen
  Regionen. Ehrlich sagen: die ⭐-Projekte sind von Hand nachgetragen bzw. von Hand aus den Unterlagen ausgewertet,
  die das Radar selbst geladen hat – sie zeigen, wie die KI-Ergebnisse aussehen.
- Tab „🔥 Jetzt anrufen“: Berliner Allee (198 WE, im Bau), Liesel-Hünichen-Weg (69 WE), Mehrfamilienhaus
  Wiesenstraße (Bauantrag). „Quartier Kösliner Weg“ ist dasselbe Projekt wie „Nordlicht“ (Instone, 198 WE) –
  einmal aus dem Rat, einmal aus der Presse.
- Tab „📋 Alle Vorhaben“, Abschnitt „Aus Ratsunterlagen“: Garstedter Tor (250 WE, B-Plan beschlossen), Ohepark (200 WE), 7 Eichen (500 WE), Postgelände
  (100 % gefördert) … mit Spalte „Stand“ (wie weit vom Küchenbedarf entfernt) und „Prüfauftrag“ (was der
  Vertrieb klären muss). Link „Quelle“ öffnet das Originaldokument.
- Neu aus der Handauswertung (24.11.): **Glashütter Markt – 112 Wohnungen, alle gefördert** (davon 38 „Junges
  Wohnen“), **Wohnpark Tarpenwiesen** am Kreuzweg (ca. 78 WE, Hälfte gefördert, dazu Seniorenanlage, Semmelhaack),
  **Buckhörner Moor / Pappelstieg** (ca. 100 WE).
- Export: „Leads als Excel herunterladen“ / PDF-Report für den Außendienst.

## 4. Frischer Hinweis (1 Min)

- Pressemitteilung vom 13.11.2026, vom Radar selbst geladen: Mehrfamilienhaus **Friedrich-Ebert-Straße /
  Friedrichsgaber Weg** (Garstedt). Die Ratsunterlagen dazu (B-Plan 364) nennen **etwa 43 Wohnungen, davon 50 %
  gefördert, Projektträger FLUWOG**. Infoabend am 25.11.2026, 18:30 Uhr, Rathaus.
- Kernsatz: **„Pressemitteilung und Ratsunterlage zusammen ergeben den Lead: wer baut, wie viel, wann.“**

## 5. Die Zahl zum Schluss (2 Min) – Ausgabe von `python -m radar recall`

- Hummel kannte 18 Norderstedter Projekte. **Das Radar findet heute 1 von 18 ganz allein. Für 13 von 18 hat es
  die Unterlagen schon geladen – genau die braucht die KI-Auswertung.** Wie viele davon die KI dann tatsächlich
  erkennt, misst der Pilot (noch nicht gemessen).
- Die übrigen 5: nie im Rat behandelt (privat), nur in der Presse oder noch ohne WE-Zahl.
- Zusatz: in den 451 Unterlagen, die auf die KI warten, stecken **11 weitere Projekte, die nicht auf Hummels Liste
  standen** (von Hand ausgewertet, siehe docs/HANDAUSWERTUNG.md) – genau das soll die KI künftig jede Woche selbst
  finden.

## 6. Die Entscheidung (3 Min)

- Volle KI-Auswertung der rund 450 wartenden Unterlagen: **geschätzt 10–15 $ einmalig**, danach geschätzt wenige
  Dollar im Monat für die neuen Unterlagen (noch nicht gemessen). Bis dahin geht es auch von Hand (wie am 24.11.),
  aber nur mit Begleitung – nicht automatisch jede Woche.
- Vorschlag: **Pilot mit 5 $ Guthaben** – nur die Unterlagen der 13 bekannten Projekte. Ergebnis: wie viele der 18
  das Radar dann selbst erkennt.
- Weitere Kommunen (Hamburger Umland, Schleswig-Holstein) über dieselben Bausteine.

## Was man nicht zeigen sollte

- Den Demo-Modus nicht als „echte Ergebnisse des Radars“ verkaufen – die ⭐-Projekte sind geprüft, aber von Hand
  eingetragen bzw. ausgewertet, die übrigen 9 sind erfunden („Demo (erfunden)“).
- Den Knopf „Lauf jetzt starten“ während der Präsentation: ein Backfill kann lange dauern.
