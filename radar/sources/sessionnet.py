"""Adapter für Somacos SessionNet (z. B. Norderstedt). Struktur verifiziert am 19.09.2026
durch echte, ratenbegrenzte Abrufe gegen buergerinfo.norderstedt.de (SessionNet 5.5.4 KP1).

Ablauf: `info.php` einmal aufrufen (setzt das Session-Cookie), danach pro Monat den Kalender
`si0040.php?__cjahr=<J>&__cmonat=<M>&__canz=1&__cselect=0` abrufen. Jede Kalenderzeile ist ein
Tag; Sitzungen stehen in der Zelle mit Klasse "silink" (mit oder ohne Detaillink, siehe unten),
abgesagte Sitzungen in einer Zelle mit Klasse "tename" ("Die Sitzung des ... fällt aus.").

Nicht jede Sitzung hat eine Detailseite (z. B. "Kinder- und Jugendbeirat" im September 2026):
dann fehlt der Link `a.smc_datatype_si`, die Zeile bleibt aber eine reguläre Sitzung (nur ohne
`url`/Unterlagen). Die Detailseite `si0057.php?__ksinr=<ID>` listet die Tagesordnung als Tabelle
`table.smctablesitzung`: jede Zeile mit `span.badge` ist ein Tagesordnungspunkt, öffentliche
Punkte beginnen mit "Ö". Vorlagen-Link `a.smc_datatype_vo` (`vo0050.php?__kvonr=<ID>`) und alle
zugehörigen Dokumente (`div.smc-d-el` mit `a[href^=getfile.php]`) stehen direkt in derselben
Zeile – ein Abgleich mit der Vorlagen-Detailseite (Stichprobe TOP "A 26/0338") ergab exakt
dieselbe Dokumentmenge, ein zusätzlicher Abruf von `vo0050.php` ist für die Dokumentliste also
nicht nötig. Sitzungsweite Dokumente (Einladung/Bekanntmachung/Niederschrift) stehen als
`div.smc-d-el` außerhalb dieser Tabelle.

Fingerprint: `<meta name="sessionnet" ...>` im `<head>` (siehe docs/QUELLENKATALOG.md).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from radar.committees import normalize
from radar.sources.base import Dokument, Sitzung, SourceAdapter, SourceLayoutError

logger = logging.getLogger(__name__)

DEFAULT_PAGES = {
    "entry_page": "info.php",
    "calendar_page": "si0040.php",
}

# Kürzel aus dem Dokument-Icon (`i.smc-doc-symbol`) -> Dokument.typ. Unbekannte Kürzel und
# Dokumente ohne Icon (z. B. Anlagen) werden per Titel-Stichwort bzw. auf "unbekannt" abgebildet.
DOC_TYPE_BY_KUERZEL = {
    "BM": "bekanntmachung",
    "ÖN": "niederschrift",
    "N": "niederschrift",
    "EI": "einladung",
    "VO": "vorlage",
}

_CANCELLED_RE = re.compile(r"Die Sitzung des (.+?) fällt aus\.?", re.IGNORECASE)


class SessionNetAdapter(SourceAdapter):
    system = "sessionnet"

    def __init__(self, source, http=None):
        super().__init__(source, http)
        self._session_ready = False

    # -- öffentliche Schnittstelle ----------------------------------------------------------
    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        self._ensure_session()
        sessions: list[Sitzung] = []
        for year, month in _iter_months(start, end):
            sessions.extend(self._list_sessions_for_month(year, month, start, end))
        return sessions

    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        if sitzung.url is None:
            return []  # keine Detailseite -> keine Unterlagen abrufbar (Normalfall)
        self._ensure_session()
        response = self.http.get(sitzung.url)
        soup = BeautifulSoup(response.text, "lxml")
        self._verify_fingerprint(soup)

        top_table = soup.find("table", class_="smctablesitzung")
        if top_table is None:
            raise SourceLayoutError(
                f"{self.source.id}: Tagesordnungstabelle nicht gefunden (si0057, {sitzung.external_id})"
            )

        # Dokumente außerhalb der Tagesordnungstabelle sind sitzungsweit (Einladung, öffentliche
        # Bekanntmachung, Niederschrift), keinem Tagesordnungspunkt/keiner Vorlage zugeordnet.
        in_table = {id(el) for el in top_table.find_all("div", class_="smc-d-el")}
        documents: list[Dokument] = []
        seen_ids: set[str] = set()

        for el in soup.find_all("div", class_="smc-d-el"):
            if id(el) in in_table:
                continue
            doc = self._parse_doc_element(el, sitzung.external_id, vorlage_id=None)
            if doc is not None and doc.external_id not in seen_ids:
                documents.append(doc)
                seen_ids.add(doc.external_id)

        for row in top_table.find_all("tr", class_="smc-t-r-l"):
            badge = row.find("span", class_="badge")
            if badge is None:
                continue  # Abschnittsüberschrift, z. B. "Öffentlicher Teil:"
            if not badge.get_text(strip=True).startswith("Ö"):
                continue  # nichtöffentlich -> nicht erfassen (CLAUDE.md: nur öffentliche Unterlagen)

            vorlage_id = None
            vorlage_nr = None
            vo_link = row.find("a", class_="smc_datatype_vo")
            if vo_link is not None:
                vorlage_id = parse_qs(urlparse(vo_link["href"]).query).get("__kvonr", [None])[0]
                vorlage_nr = normalize(vo_link.get_text(strip=True)) or None

            docs_cell = row.find("td", class_="sidocs")
            if docs_cell is None:
                continue
            for el in docs_cell.find_all("div", class_="smc-d-el"):
                doc = self._parse_doc_element(el, sitzung.external_id, vorlage_id, vorlage_nr)
                if doc is not None and doc.external_id not in seen_ids:
                    documents.append(doc)
                    seen_ids.add(doc.external_id)

        return documents

    # -- intern -------------------------------------------------------------------------------
    def _ensure_session(self) -> None:
        """`info.php` setzt das Session-Cookie; ohne vorherigen Aufruf liefert z. B. si0057.php
        eine SessionNet-Fehlermeldung statt Inhalt. Pro Adapter-Instanz nur einmal nötig."""
        if not self._session_ready:
            entry_page = self.source.options.get("entry_page", DEFAULT_PAGES["entry_page"])
            self.http.get(entry_page)
            self._session_ready = True

    def _verify_fingerprint(self, soup: BeautifulSoup) -> None:
        if soup.find("meta", attrs={"name": "sessionnet"}) is None:
            raise SourceLayoutError(
                f"{self.source.id}: Seite enthält kein SessionNet-Fingerprint-Meta-Tag "
                "(Layoutwechsel oder falsches System? siehe docs/QUELLENKATALOG.md)"
            )

    def _list_sessions_for_month(self, year: int, month: int, start: date, end: date) -> list[Sitzung]:
        calendar_page = self.source.options.get("calendar_page", DEFAULT_PAGES["calendar_page"])
        url = f"{calendar_page}?__cjahr={year}&__cmonat={month}&__canz=1&__cselect=0"
        response = self.http.get(url)
        soup = BeautifulSoup(response.text, "lxml")
        self._verify_fingerprint(soup)

        table = soup.find("table", id=re.compile(r"si0040_contenttable"))
        if table is None:
            raise SourceLayoutError(f"{self.source.id}: Kalendertabelle nicht gefunden (si0040, {year}-{month:02d})")
        rows = table.find_all("tr")
        if not rows:
            raise SourceLayoutError(f"{self.source.id}: Kalendertabelle leer (si0040, {year}-{month:02d})")

        sessions = []
        for row in rows:
            sitzung = self._parse_calendar_row(row, year, month)
            if sitzung is not None and start <= sitzung.datum <= end:
                sessions.append(sitzung)
        return sessions

    def _parse_calendar_row(self, row, year: int, month: int) -> Sitzung | None:
        day_cell = row.find("td", class_="smc_fct_day")
        if day_cell is None:
            return None
        day_text = day_cell.get_text(strip=True)
        if not day_text.isdigit():
            return None  # Leerzeile zwischen Kalenderwochen, kein echter Tag
        datum = date(year, month, int(day_text))

        cancel_cell = row.find("td", class_="tename")
        if cancel_cell is not None:
            return self._parse_cancelled_row(cancel_cell, datum)

        session_cell = row.find("td", class_="silink")
        if session_cell is None:
            return None  # Tag ohne jede Sitzung
        return self._parse_session_cell(session_cell, datum)

    def _parse_cancelled_row(self, cancel_cell, datum: date) -> Sitzung | None:
        text = normalize(cancel_cell.get_text(" ", strip=True))
        if not text:
            return None
        match = _CANCELLED_RE.match(text)
        # Genitivform ("...ausschusses") lässt sich nicht verlustfrei in den Nominativ zurückwandeln;
        # der Ausschussfilter (radar.committees) sucht ohnehin per Teilstring, das genügt für die
        # gängigen Muster (z. B. "stadtentwicklung" bleibt in "Stadtentwicklungsausschusses" erhalten).
        gremium = match.group(1) if match else text
        external_id = f"abgesagt-{datum.isoformat()}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"
        return Sitzung(source_id=self.source.id, external_id=external_id, gremium=gremium, datum=datum, abgesagt=True)

    def _parse_session_cell(self, session_cell, datum: date) -> Sitzung | None:
        name_div = session_cell.find("div", class_="smc-el-h")
        if name_div is None:
            return None
        gremium = normalize(name_div.get_text(strip=True))
        if not gremium:
            return None

        link = name_div.find("a", class_="smc_datatype_si")
        url = None
        external_id = None
        if link is not None and link.get("href"):
            url = link["href"]
            external_id = parse_qs(urlparse(url).query).get("__ksinr", [None])[0]
        if external_id is None:
            # Keine Detailseite veröffentlicht (Normalfall, siehe CLAUDE.md) -> stabile
            # synthetische ID aus Datum und Gremiumsnamen statt einer fehlenden __ksinr.
            slug = re.sub(r"[^a-z0-9]+", "-", gremium.lower()).strip("-")
            external_id = f"{datum.isoformat()}-{slug}"

        uhrzeit = None
        ort = None
        detail_list = session_cell.find("ul", class_="smc-detail-list")
        if detail_list is not None:
            items = detail_list.find_all("li")
            if len(items) >= 1:
                uhrzeit = normalize(items[0].get_text(strip=True))
            if len(items) >= 2:
                ort = normalize(items[1].get_text(strip=True))

        return Sitzung(
            source_id=self.source.id,
            external_id=external_id,
            gremium=gremium,
            datum=datum,
            uhrzeit=uhrzeit,
            ort=ort,
            url=url,
        )

    def _parse_doc_element(
        self, el, sitzung_external_id: str, vorlage_id: str | None, vorlage_nr: str | None = None
    ) -> Dokument | None:
        links = el.find_all("a", href=re.compile(r"^getfile\.php"))
        text_link = next((a for a in links if a.get_text(strip=True)), links[0] if links else None)
        if text_link is None:
            return None
        href = text_link["href"]
        doc_id = parse_qs(urlparse(href).query).get("id", [None])[0]
        if doc_id is None:
            return None
        titel = normalize(text_link.get_text(strip=True)) or "(ohne Titel)"

        symbol = el.find(class_="smc-doc-symbol")
        kuerzel = symbol.get_text(strip=True) if symbol else ""
        typ = DOC_TYPE_BY_KUERZEL.get(kuerzel) or ("anlage" if "anlage" in titel.lower() else "unbekannt")

        return Dokument(
            source_id=self.source.id,
            external_id=doc_id,
            titel=titel,
            url=href,
            sitzung_external_id=sitzung_external_id,
            vorlage_external_id=vorlage_id,
            vorlage_nr=vorlage_nr,
            typ=typ,
        )


def _iter_months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1
