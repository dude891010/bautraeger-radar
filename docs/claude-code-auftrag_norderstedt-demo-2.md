# Auftrag für Claude Code: 8 weitere Norderstedt-Projekte in `data/test_radar.db`

> Im Terminal im Ordner `bautraeger-radar` starten (`claude`) und alles ab „AUFTRAG" einfügen.

---

## AUFTRAG

Ergänze die Demo-Daten in `data/test_radar.db` um die 8 Projekte unten. Nutze dafür denselben Seed-Mechanismus wie beim letzten Mal (`scripts/seed_demo_norderstedt.py` oder das bestehende Seed-Skript), idempotent, ohne Duplikate.

### Wichtig: Herkunft kennzeichnen
Diese Projekte hat **nicht** das Radar automatisch gefunden. Sie wurden **manuell nachrecherchiert**, nachdem Hummel Küchen seine Liste geschickt hat. Speichere das sauber:
- Falls es ein Feld für Herkunft/Quelle-Typ gibt: Wert `manuell_nachgetragen`.
- Falls nicht: lege ein solches Feld an (Migration, Default `radar` für alle bestehenden Einträge) und zeig es im Dashboard als kleines Badge „manuell nachgetragen“.
- Phase und WE wie bei den anderen Demo-Projekten anzeigen. Bei `wohneinheiten: null` „WE offen“ anzeigen, keine Zahl erfinden.

### Duplikate
Vor dem Einfügen prüfen, ob ein Projekt schon existiert (Adresse/Straße/B-Plan-Nummer). Nordlicht, SonnenHeyde und Richtweg nicht anfassen. **Garstedter Tor (B-Plan 337) ist NICHT Nordlicht**, sondern ein eigenes Projekt. Falls es durch eine frühere Ausschlussregel blockiert wird, diese Regel korrigieren.

### Danach
`pytest` (alles grün) und `ruff check .` (sauber). Zeig mir per SQL die 8 neuen Zeilen mit Name, Phase, WE, Herkunft und Quell-URL.

### Projektdaten

```json
[
  {
    "name": "Garstedter Tor (B-Plan 337)",
    "bauherr": "GARTO GmbH & Co. KG (laut Stadtunterlagen); laut Presse Plambeck",
    "adresse": "zwischen Kohfurth und Stettiner Straße, 22850 Norderstedt",
    "ortsteil": "Garstedt",
    "wohneinheiten": 250,
    "we_hinweis": "ca. 250 WE, 30 % gefördert, mehrere Bauabschnitte",
    "phase": "B-Plan",
    "phase_detail": "Entwurf öffentlich ausgelegt bis 22.10.2024",
    "stand_datum": "2024-09-26",
    "quellen": [
      {"titel": "Stadt Norderstedt: PM B-Plan 337 „Garstedter Tor“", "url": "https://www.norderstedt.de/media/custom/3224_9358_1.PDF"},
      {"titel": "Stadt Norderstedt: Anlage GARTO (Garstedter Tor) im B-Plan 337", "url": "https://norderstedt.de/media/custom/3224_9229_1.PDF"}
    ],
    "herkunft": "manuell_nachgetragen"
  },
  {
    "name": "7 Eichen",
    "bauherr": "offen",
    "adresse": "zwischen Glashütter Damm und Schleswig-Holstein-Straße, 22851 Norderstedt",
    "ortsteil": "Glashütte",
    "wohneinheiten": 500,
    "we_hinweis": "zunächst rund 500 Wohnungen auf 17 ha (Hummel: 300 WE – abgleichen)",
    "phase": "in Planung",
    "phase_detail": "Vorentwurf vorgestellt; B-Plan-Verfahren läuft",
    "stand_datum": "2026-11-13",
    "quellen": [
      {"titel": "noa4: „7 Eichen“: Anwohner fürchten den Verkehrskollaps", "url": "https://www.noa4.de/7-eichen-anwohner-fuerchten-den-verkehrskollaps/"}
    ],
    "herkunft": "manuell_nachgetragen"
  },
  {
    "name": "Ohepark – B-Plan 322 „An der Ohechaussee“",
    "bauherr": "Projektentwickler (nicht namentlich genannt)",
    "adresse": "Ohechaussee / Robert-Schumann-Straße, 22848 Norderstedt",
    "ortsteil": "Garstedt",
    "wohneinheiten": 200,
    "we_hinweis": "etwa 200 WE in neun Gebäuden, mind. 50 % gefördert, Kita",
    "phase": "B-Plan",
    "phase_detail": "Entwurf vorgestellt 05.11.2024; FNP-Änderung Stand 29.01.2026",
    "stand_datum": "2026-01-29",
    "quellen": [
      {"titel": "Stadt Norderstedt: PM B-Plan 322 (30.10.2024)", "url": "https://www.norderstedt.de/media/custom/3224_9563_1.PDF"},
      {"titel": "Stadt Norderstedt: Begründung 18. FNP-Änderung (Stand 29.01.2026)", "url": "https://www.norderstedt.de/media/custom/3224_13821_1.PDF"}
    ],
    "herkunft": "manuell_nachgetragen"
  },
  {
    "name": "Rahmenplan Harkshörner Weg",
    "bauherr": "offen",
    "adresse": "Harkshörner Weg / Ulzburger Straße, 22844 Norderstedt",
    "ortsteil": "Harksheide",
    "wohneinheiten": 500,
    "we_hinweis": "rund 500 WE",
    "phase": "in Planung",
    "phase_detail": "Rahmenplan, Beteiligung bis 12.05.2025; Übergang in B-Plan",
    "stand_datum": "2025-04-03",
    "quellen": [
      {"titel": "Stadt Norderstedt: PM Rahmenplan Harkshörner Weg (03.04.2025)", "url": "https://www.norderstedt.de/media/custom/3224_10531_1.PDF"}
    ],
    "herkunft": "manuell_nachgetragen"
  },
  {
    "name": "Segeberger Chaussee 121–125 (REWE + Wohnen)",
    "bauherr": "Investor (nicht namentlich genannt)",
    "adresse": "Segeberger Chaussee 121–125, 22850 Norderstedt",
    "ortsteil": "Harksheide",
    "wohneinheiten": 35,
    "we_hinweis": "35 WE über REWE-Markt (Hummel: 91 WE – abgleichen)",
    "phase": "in Planung",
    "phase_detail": "ASV-Entscheidung über das Projekt angesetzt",
    "stand_datum": "2025-11-20",
    "quellen": [
      {"titel": "noa4: Neues Quartier an der Segeberger Chaussee", "url": "https://www.noa4.de/neues-quartier-an-der-segeberger-chaussee-rewe-markt-mit-wohnungen/"}
    ],
    "herkunft": "manuell_nachgetragen"
  },
  {
    "name": "Postgelände Falkenbergstraße",
    "bauherr": "ADLERSHORST Baugenossenschaft eG (laut Hummel)",
    "adresse": "Falkenbergstraße 17, 22844 Norderstedt",
    "ortsteil": "Harksheide",
    "wohneinheiten": 100,
    "we_hinweis": "rund 100 WE, mind. 50 % gefördert",
    "phase": "in Planung",
    "phase_detail": "ASV hat nächste Planungsrunde beschlossen; B-Plan-Entwürfe folgen",
    "stand_datum": "2026-09-21",
    "quellen": [
      {"titel": "noa4: 100 neue Wohnungen auf dem Postgelände", "url": "https://www.noa4.de/100-neue-wohnungen-auf-dem-postgelaende/"}
    ],
    "herkunft": "manuell_nachgetragen"
  },
  {
    "name": "Hermann-Klingenberg-Ring („Norderstedter Modell“)",
    "bauherr": "Stadt Norderstedt",
    "adresse": "Hermann-Klingenberg-Ring / Lawaetzstraße, 22844 Norderstedt",
    "ortsteil": "Friedrichsgabe",
    "wohneinheiten": 40,
    "we_hinweis": "40 geförderte WE in 4 Gebäuden",
    "phase": "in Planung",
    "phase_detail": "ZURÜCKGESTELLT: Hauptausschuss empfiehlt Stopp wegen Haushaltslage; Stadtvertretung entscheidet",
    "stand_datum": "2026-09-29",
    "quellen": [
      {"titel": "noa4: „Norderstedter Modell“ vorerst auf Eis gelegt", "url": "https://www.noa4.de/norderstedter-modell-vorerst-auf-eis-gelegt/"}
    ],
    "herkunft": "manuell_nachgetragen"
  },
  {
    "name": "Liesel-Hünichen-Weg (ADLERSHORST)",
    "bauherr": "ADLERSHORST Baugenossenschaft eG",
    "adresse": "Liesel-Hünichen-Weg, 22844 Norderstedt",
    "ortsteil": "Harksheide",
    "wohneinheiten": 69,
    "we_hinweis": "69 geförderte WE (1. und 2. Förderweg), 2–4 Zimmer, Tiefgarage",
    "phase": "im Bau",
    "phase_detail": "Spatenstich erfolgt; Vermietung ab Q2 2027, Fertigstellung Q4 2027",
    "stand_datum": "2026-11-13",
    "quellen": [
      {"titel": "noa4: Spatenstich für 69 geförderte Wohnungen", "url": "https://www.noa4.de/spatenstich-fuer-69-gefoerderte-wohnungen-in-norderstedt/"}
    ],
    "herkunft": "manuell_nachgetragen"
  }
]
```
