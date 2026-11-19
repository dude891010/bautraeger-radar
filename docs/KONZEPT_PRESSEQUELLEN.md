# Konzept: Presse als zusätzliche Quelle (Phase 6, Stand 20.11.2026)

**Quelle A ist umgesetzt** (siehe CLAUDE.md, „Phase 6“). Quelle B (noa4.de) ist weiterhin nur Konzept.

## Warum

Der Hummel-Abgleich (CLAUDE.md, „Hummel-Abgleich Norderstedt“) zeigte: Einige Projekte stehen nie in den
Ratsunterlagen, weil sie privat genehmigt werden (z. B. Liesel-Hünichen-Weg, 69 geförderte WE, Spatenstich 08/2026).
Andere stehen dort, aber die Presse meldet sie früher oder verständlicher. Zwei Quellen wurden geprüft.

## Quelle A: Pressemitteilungen der Stadt Norderstedt – ✅ umgesetzt am 20.11.2026 (Quelle `norderstedt_presse`)

**Fundort:** <https://www.norderstedt.de/Aktuelles-und-Service/Aktuelles/Pressestelle-/> – eine einzige Seite mit
**736 Pressemitteilungen** als PDF-Links (`/media/custom/3224_<ID>_1.PDF?<Zeitstempel>`), neueste zuerst, jeweils mit
Titel. Der Zahlenanhang `?1795078088` ist ein Unix-Zeitstempel (= 19.11.2026, vermutlich Hochlade-/Änderungszeit,
nicht zwingend das Datum der Meldung) – als Sortier- und Näherungsdatum ohne Extra-Abruf brauchbar.

**Rechtlich/höflich:** robots.txt sperrt nur `/cache/`, `/output/`, Suche und Weiterleitungen; Pressemitteilungen sind
zur Weiterverbreitung gedacht. Gleicher Host wie die schon geprüften B-Plan-PDFs (`norderstedt.de/media/custom/...`).

**Technik (passt ohne Schemaänderung in die vorhandene Pipeline):**
- Neuer Adapter-Typ `pressemitteilungen` (generisch: Listen-URL und Link-Muster in `config/sources.yaml`, kein
  Hardcoding), eine neue Quelle `norderstedt_presse`.
- Je Pressemitteilung eine `Sitzung` (Datum aus dem Zeitstempel, Gremium „Pressemitteilung“) mit genau einem
  `Dokument` (das PDF) und der PM-ID als `vorlage_external_id` – damit laufen Download, PDF-Prüfung, Text-Cache,
  Stichwort-Vorfilter, KI-Auswertung, Datenbank, Dashboard und Recall unverändert.
- **Titel-Vorfilter vor dem Download:** nur Titel mit Wohnbau-Begriffen (Wohnraum, Wohnungen, B-Plan,
  Bebauungsplan, Rahmenplan, Quartier, Spatenstich, Richtfest …, **mit Wortgrenzen** – ohne sie trifft „Wohn“ auch
  „gewohnt“). Probe: 40 von 736 Titeln, nach Bereinigung grob 25–30 → nur diese PDFs laden.
- Inkrementell: nur IDs größer als die höchste schon bekannte; Erstlauf ab `scan.backfill_ab`.

**Aufwand:** ca. **4–6 Stunden** inkl. Fixture (die echte Listenseite einmal gespeichert), Tests, Doku.

**Bringt sofort (auch ohne KI):** frische Hinweise wie „Neuer Wohnraum in Norderstedt-Garstedt geplant:
Informationsveranstaltung am 25. November“ (PM vom 13.11.2026) und die Auslegungs-Meldungen zu B-Plänen
(z. B. 319, 339, 343, 344, 347) früher als über die Sitzungsunterlagen.

## Quelle B: noa4.de (lokaler Sender, Rubrik „Lokal“) – in 4 Schritten

| Schritt | Inhalt | Stand |
|---|---|---|
| 1 | RSS-Feed „Lokal“ lesen, Vorfilter auf Titel/Anriss, Text = Titel + Anriss | ✅ 20.11.2026 (Quelle `noa4`) |
| 2 | Artikelseite laden, Volltext herauslösen | ✅ 20.11.2026 (`artikel_selektor`) |
| 3 | Ältere Artikel nachladen – **statt Sitemap durch tiefes Blättern im Feed** (Seite ~100 = Anfang 2025, dann 404; die Sitemap hat keine Titel/Anrisse für den Vorfilter) | ✅ 20.11.2026 (`max_seiten: 150`) |
| 4 | KI-freier Web-Fund für Spatenstich/Baubeginn | ✅ 20.11.2026 (`web_fund_bewertung`) – Liesel-Hünichen-Weg als erster echter Fund |

**Rechtlich/höflich (geprüft 20.11.2026):**
- robots.txt sperrt nur Admin-Bereich und Suche; Artikel und Feeds sind erlaubt.
- Kein Nutzungsvorbehalt für automatisches Auslesen (Text- und Data-Mining, § 44b UrhG): weder im Impressum noch
  maschinenlesbar (`/.well-known/tdmrep.json` 404, kein `tdm-reservation`-Meta-Tag).
- Urheberrecht trotzdem beachten: Artikeltext nur intern auswerten, im Dashboard nur Titel, Link und ein kurzes
  Zitat (≤ 200 Zeichen, wie überall im Radar) – **kein Volltext**. Eine kurze freundliche Info-Mail an noa4 ist
  empfehlenswert (keine Pflicht).
- Hamburger Abendblatt bleibt ausgeschlossen (Paywall, Auftrag).

**Technik:**
- RSS-Feed der Rubrik: `https://www.noa4.de/category/lokal/feed/` (10 Artikel je Seite, blätterbar mit `?paged=2`),
  liefert Titel, Datum, Link und nur einen **Anriss von ca. 160 Zeichen**.
- Sitemap `https://www.noa4.de/sitemap.xml` mit Teil-Sitemaps und Änderungsdatum – geprüft rund 1.200 Artikel ab
  01/2025 in den ersten beiden Teil-Sitemaps; Vollständigkeit beim Bau prüfen. Für den Erstlauf (Backfill).
- Ablauf: Feed bzw. Sitemap lesen → **Vorfilter auf Titel + Anriss** (Norderstedt + Wohnbau-Begriff, ohne Abruf)
  → nur passende Artikelseiten laden (2 s Pause) → Text als `Dokument.text` (wie bei bv-hh.de, `Textinhalt`) →
  dieselbe KI-Auswertung wie bei den Ratsunterlagen.
- **Ohne KI-Guthaben** gibt es einen Zwischenweg: Artikel über Baubeginn/Spatenstich/Richtfest mit der schon
  vorhandenen, KI-freien Bewertung der Web-Recherche (`radar/web_recherche.py`: Ortsname + Wohnbau-Begriff +
  Phasen-Stichwort, WE-Zahl per Muster) als Web-Fund anzeigen. Frühe Phasen (B-Plan, Rahmenplan) brauchen die KI.

**Aufwand:** ca. **6–8 Stunden** (Feed + Sitemap + Artikeltext-Erkennung + Tests); KI-freier Zwischenweg für
Baubeginn-Meldungen zusätzlich ca. **2 Stunden**.

**Bringt laut Hummel-Liste:** Liesel-Hünichen-Weg (Artikel vom 06.08.2026 verifiziert). Für **Falkenkamp 4** und
**Königsberger Straße** fand sich bei noa4 kein Artikel – Hummel fragen, woher diese Informationen stammen.

## Gemeinsam für beide Quellen

- Gefundene Projekte bekommen `herkunft = 'radar'` (automatisch gefunden) und zählen im Recall mit.
- **Offenes Risiko – Dubletten über Quellen hinweg:** dasselbe Projekt kann aus Ratsunterlagen, Pressemitteilung
  und noa4 kommen; die Dedup (`upsert_projekt`) arbeitet bisher nur innerhalb einer Quelle. Für die Demo hinnehmbar,
  für den Betrieb später eine quellenübergreifende Zuordnung über Adresse/B-Plan-Nr. (ca. 3–4 Stunden).
- Ohne KI-Guthaben werden die Texte geladen und vorgefiltert, aber nicht ausgewertet – genau wie bei den
  Ratsunterlagen. Die Recall-Zahl „Unterlagen geladen“ steigt trotzdem.

## Vorschlag zur Reihenfolge

1. Pressemitteilungen der Stadt (4–6 h) – amtlich, einfach, geringes Risiko.
2. noa4.de (6–8 h), danach optional der KI-freie Zwischenweg (2 h).
3. Später: quellenübergreifende Dubletten-Erkennung (3–4 h).

**Offene Fragen:** OK für die Umsetzung? Soll noa4 vorab informiert werden? Woher kennt Hummel Falkenkamp 4 und
Königsberger Straße?
