"""Adapter für bv-hh.de - ein inoffizielles, von einer Einzelperson gepflegtes Community-Portal
(github.com/bv-hh/bv-hh), das die öffentlichen ALLRIS-Daten aller 7 Hamburger Bezirksversammlungen
in einer einheitlichen, robots.txt-freundlichen Web-Oberfläche spiegelt. Die Original-ALLRIS-Seiten
der Bezirke sperren Crawler pauschal (`Disallow: /`, siehe CLAUDE.md "Referenzquelle
Uplengen/OParl" und config/sources.yaml) - bv-hh ist deshalb bewusst als eigene, unsichere Quelle
(kein offizieller Betreiber, keine Verfügbarkeitsgarantie) und in einem eigenen Adapter/eigener
Datei von radar/sources/hamburg_transparenz.py getrennt gehalten.

**Bewusst getrennt von radar/sources/hamburg_transparenz.py** (offizielles Transparenzportal, aber
nur auf bereits rechtskräftige Bebauungspläne beschränkt): bv-hh deckt zusätzlich die frühen
Verfahrensstufen (Aufstellungsbeschluss, Auslegung) ab, weil es direkt die
Bezirksversammlungs-Drucksachen zeigt.

**Kein PDF, nur extrahierter Text:** bv-hh spiegelt keine PDF-Anlagen (an mehreren echten
Drucksachen unterschiedlichen Typs geprüft - die "Anhänge"-Sektion war überall leer). Jede
Drucksachen-Seite enthält ihren Haupttext aber bereits sauber extrahiert im `articleBody`-Feld
eines eingebetteten JSON-LD-Blocks (schema.org/Article). `Dokument.text` wird deshalb direkt damit
befüllt statt mit einer PDF-URL - siehe radar/parser.py (`Textinhalt`) und radar/scraper.py, die
diesen Fall ohne Download behandeln. Wohneinheiten-Zahlen aus separaten Anlagen (Begründung,
städtebaulicher Vertrag) sind darüber NICHT erreichbar - unklare Fälle werden entsprechend
häufiger mit "prüfen" markiert statt einer genauen Zahl (CLAUDE.md: unklare Fälle nie
stillschweigend verwerfen).

Ablauf: `<bezirk>/meetings?page=<n>` (Liste, neueste zuerst - **eindeutig chronologisch absteigend
sortiert**, im Gegensatz zur OParl-Erfahrung sicher fürs frühe Abbrechen sobald eine Seite vor dem
Scan-Zeitraum liegt) -> je Sitzung `<bezirk>/meetings/<slug>` (Tagesordnung mit Verweisen auf
Drucksachen, Badge "Ö"/"N" wie bei SessionNet) -> je referenzierter Drucksache
`<bezirk>/documents/<slug>` (Text im JSON-LD).

Ein Adapter für alle 7 Bezirke: die Bezirks-URL steckt in `source.base_url`
(z. B. "https://bv-hh.de/bergedorf/"), alle Selektoren sind darüber hinaus generisch (Linkmuster
`/meetings/`, `/documents/`, `/committees/` unabhängig vom Bezirk).

Verifiziert am 19.09.2026 gegen die echte Bergedorf-Instanz (tests/fixtures/bv_hh/).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from html import unescape

from bs4 import BeautifulSoup

from radar.committees import normalize
from radar.sources.base import Dokument, Sitzung, SourceAdapter, SourceLayoutError

logger = logging.getLogger(__name__)

MAX_PAGES = 60  # Fail-loud-Grenze: mehr Sitzungsseiten für einen Scan-Zeitraum wären ein Warnsignal
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")


def _external_id_from_href(href: str) -> str | None:
    """bv-hh-Slugs enden auf die numerische Allris-ID, z. B. '...-bergedorf-7438' -> '7438'."""
    tail = href.rstrip("/").rsplit("-", 1)[-1]
    return tail if tail.isdigit() else None


class BvHhAdapter(SourceAdapter):
    system = "bv_hh"

    # -- öffentliche Schnittstelle ------------------------------------------------------------
    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        sessions: list[Sitzung] = []
        page = 1
        while True:
            soup = self._get_soup(f"meetings?page={page}")
            rows = soup.select("div.row.pt-3.border-top")
            if not rows:
                if page == 1:
                    raise SourceLayoutError(f"{self.source.id}: Sitzungsliste leer (meetings?page=1) - Layoutwechsel?")
                break

            aelter_als_start = False
            for row in rows:
                sitzung = self._parse_meeting_row(row)
                if sitzung is None:
                    continue
                if sitzung.datum < start:
                    aelter_als_start = True
                    break  # Liste ist absteigend sortiert: alles Weitere ist noch älter
                if sitzung.datum <= end:
                    sessions.append(sitzung)
            if aelter_als_start:
                break

            page += 1
            if page > MAX_PAGES:
                raise SourceLayoutError(
                    f"{self.source.id}: mehr als {MAX_PAGES} Sitzungsseiten für {start}..{end} "
                    "(Sortierung/Layout geprüft?)"
                )
        return sessions

    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        if sitzung.url is None:
            return []
        soup = self._get_soup(sitzung.url)
        rows = soup.select("div.row.pt-3.border-top")
        if not rows:
            raise SourceLayoutError(f"{self.source.id}: Tagesordnung leer ({sitzung.url}) - Layoutwechsel?")

        documents: list[Dokument] = []
        seen: set[str] = set()
        for row in rows:
            doc = self._parse_agenda_row(row, sitzung.external_id)
            if doc is not None and doc.external_id not in seen:
                documents.append(doc)
                seen.add(doc.external_id)
        return documents

    # -- intern ---------------------------------------------------------------------------------
    def _parse_meeting_row(self, row) -> Sitzung | None:
        meeting_link = row.select_one("a[href*='/meetings/']")
        committee_link = row.select_one("a[href*='/committees/']")
        if meeting_link is None or committee_link is None:
            return None

        href = meeting_link["href"]
        external_id = _external_id_from_href(href)
        date_match = _DATE_RE.search(meeting_link.get_text(" ", strip=True))
        if external_id is None or date_match is None:
            return None
        tag, monat, jahr = date_match.groups()
        datum = date(int(jahr), int(monat), int(tag))

        gremium = normalize(committee_link.get_text(strip=True))
        if not gremium:
            return None

        uhrzeit = None
        parent = meeting_link.find_parent("div")
        if parent is not None:
            zeit_match = _TIME_RE.search(normalize(parent.get_text(" ", strip=True)))
            if zeit_match:
                uhrzeit = zeit_match.group(1)

        return Sitzung(
            source_id=self.source.id, external_id=external_id, gremium=gremium, datum=datum, uhrzeit=uhrzeit, url=href
        )

    def _parse_agenda_row(self, row, sitzung_external_id: str) -> Dokument | None:
        cols = row.find_all("div", recursive=False)
        if len(cols) < 3:
            return None
        badge_col, title_col, drs_col = cols[0], cols[1], cols[2]

        badge_text = normalize(badge_col.get_text(" ", strip=True))
        if not badge_text.startswith("Ö"):
            return None  # nichtöffentlich oder kein erkennbares Badge -> nicht erfassen

        doc_link = title_col.select_one("a[href*='/documents/']")
        if doc_link is None:
            return None  # rein prozeduraler Tagesordnungspunkt ohne Drucksache

        href = doc_link["href"]
        external_id = _external_id_from_href(href)
        if external_id is None:
            return None
        titel = normalize(doc_link.get_text(" ", strip=True)) or "(ohne Titel)"
        drs_link = drs_col.select_one("a")
        vorlage_nr = normalize(drs_link.get_text(strip=True)) if drs_link else None

        text = self._document_text(href)
        if text is None:
            return None  # kein articleBody gefunden - nichts Auswertbares (Quelle liefert nicht immer Inhalt mit)

        return Dokument(
            source_id=self.source.id,
            external_id=external_id,
            titel=titel,
            url=href,
            sitzung_external_id=sitzung_external_id,
            vorlage_external_id=external_id,
            vorlage_nr=vorlage_nr,
            typ="drucksache",
            text=text,
        )

    def _document_text(self, href: str) -> str | None:
        soup = self._get_soup(href)
        script = soup.find("script", type="application/ld+json")
        if script is None or not script.string:
            return None
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            return None
        body = data.get("articleBody")
        if not body:
            return None
        return unescape(normalize(body))

    def _get_soup(self, url: str) -> BeautifulSoup:
        response = self.http.get(url)
        soup = BeautifulSoup(response.text, "lxml")
        brand = soup.select_one("a.navbar-brand")
        if brand is None or "BV-HH" not in brand.get_text(strip=True):
            raise SourceLayoutError(f"{self.source.id}: Seite sieht nicht nach bv-hh.de aus ({url}) - Layoutwechsel?")
        return soup
