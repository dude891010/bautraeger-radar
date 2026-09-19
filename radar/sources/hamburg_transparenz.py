"""Adapter für das Transparenzportal Hamburg (CKAN-API), Register `bauleitplaene`.

Kein Ratsinformationssystem: Das Hamburgische Transparenzgesetz verpflichtet die Stadt, u. a. alle
rechtskräftigen Bauleitpläne über eine öffentliche CKAN-API zu veröffentlichen - unter offener
Lizenz (Datenlizenz Deutschland) inklusive der tatsächlichen PDF-Dokumente (Begründung,
Festsetzungen, amtliche Bekanntmachung), gehostet auf weiteren Hamburg-Domains
(`daten-hamburg.de`, `archiv.transparenz.hamburg.de` - je nach Alter des Plans unterschiedlich,
siehe `additional_hosts` in config/sources.yaml).

**Kein Sitzungskonzept:** jede Veröffentlichung (ein CKAN-"Package") wird deshalb 1:1 auf eine
synthetische `Sitzung` abgebildet (Gremium fest "Bauleitplanung", Datum = `publishing_date`), die
zugleich die einzige Vorlage dieser "Sitzung" ist - reine Wiederverwendung des bestehenden
Sitzung/Vorlage/Dokument-Modells (radar/sources/base.py), kein Sonderfall in Parser, Datenbank
oder Dashboard nötig.

**Deckt nur die letzte Verfahrensstufe ab:** `registerobject_type: bauleitplaene` erfasst nur
bereits rechtskräftige (im Amtlichen Anzeiger/HmbGVBl veröffentlichte) Pläne, also den
Satzungsbeschluss. Frühere, für den Vertrieb oft wertvollere Stufen (Aufstellungsbeschluss,
Auslegung) stehen hier nicht - dafür siehe `radar/sources/bv_hh.py` (inoffizielle Ergänzung für
die Bezirksversammlungen selbst, bewusst als eigener Adapter/eigene Quelle getrennt).

**robots.txt-Sonderfall:** `suche.transparenz.hamburg.de` sperrt `/api/` pauschal, dokumentiert
und lizenziert dieselbe API aber selbst ausdrücklich zur automatisierten Nutzung - deshalb
`ignore_robots_txt: true` für diese eine Quelle (Begründung in `config/sources.yaml`, siehe auch
CLAUDE.md). Die Datei-Hosts (`daten-hamburg.de`, `archiv.transparenz.hamburg.de`) haben keine
eigene robots.txt und werden über `additional_hosts` unabhängig und ganz normal geprüft.

**Paginierung:** `metadata_modified` (Zeitpunkt des letzten nächtlichen Harvest-Laufs) korreliert
NICHT mit dem tatsächlichen `publishing_date` - ein sortierungsbasiertes Abbrechen wäre also
falsch (siehe CLAUDE.md, Erfahrung von der OParl-Prüfung). Stattdessen ein direkter
Solr-Bereichsfilter auf `publishing_date` (gegen echte Antworten geprüft, filtert korrekt) mit
normaler CKAN-Seitenpaginierung (`start`/`rows`) über die dadurch üblicherweise kleine Trefferliste.

Verifiziert am 19.09.2026 gegen die echte, öffentliche API (Fixtures unter
tests/fixtures/hamburg_transparenz/).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from urllib.parse import urlencode

from radar.committees import normalize
from radar.sources.base import Dokument, Sitzung, SourceAdapter, SourceLayoutError

logger = logging.getLogger(__name__)

REGISTEROBJECT_TYPE = "bauleitplaene"
PAGE_SIZE = 100
MAX_RESULTS = 5000  # Fail-loud-Grenze: mehr Treffer in einem Scan-Zeitraum wären ein Warnsignal

# Titel-Stichwörter -> Dokument.typ (rein beschreibend, keine Filterlogik).
_TYP_KEYWORDS = (
    ("begründung", "begruendung"),
    ("bekanntmachung", "bekanntmachung"),
    ("festsetzung", "festsetzung"),
    ("planzeichnung", "festsetzung"),
)


class HamburgTransparenzAdapter(SourceAdapter):
    system = "hamburg_transparenz"

    # -- öffentliche Schnittstelle ------------------------------------------------------------
    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        sessions: list[Sitzung] = []
        offset = 0
        while True:
            result = self._search(start, end, rows=PAGE_SIZE, start_row=offset)
            results = result.get("results") or []
            for pkg in results:
                sitzung = self._parse_package_as_sitzung(pkg)
                if sitzung is not None:
                    sessions.append(sitzung)
            offset += len(results)
            total = result.get("count", 0)
            if not results or offset >= total:
                break
            if offset > MAX_RESULTS:
                raise SourceLayoutError(
                    f"{self.source.id}: mehr als {MAX_RESULTS} Treffer für {start}..{end} "
                    "(Filter kaputt oder Layoutwechsel?)"
                )
        return sessions

    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        if sitzung.url is None:
            return []
        payload = self._get_json(sitzung.url)
        if not payload.get("success"):
            raise SourceLayoutError(
                f"{self.source.id}: package_show meldete einen Fehler für {sitzung.external_id}: {payload}"
            )
        pkg = payload["result"]

        documents: list[Dokument] = []
        for resource in pkg.get("resources") or []:
            doc = self._parse_resource(resource, sitzung.external_id)
            if doc is not None:
                documents.append(doc)
        return documents

    # -- intern ---------------------------------------------------------------------------------
    def _search(self, start: date, end: date, *, rows: int, start_row: int) -> dict:
        fq = (
            f"registerobject_type:{REGISTEROBJECT_TYPE} AND "
            f"publishing_date:[{start.isoformat()}T00:00:00Z TO {end.isoformat()}T23:59:59Z]"
        )
        query = urlencode({"q": "*:*", "fq": fq, "rows": rows, "start": start_row})
        payload = self._get_json(f"{self.source.base_url}package_search?{query}")
        if not payload.get("success"):
            raise SourceLayoutError(f"{self.source.id}: package_search meldete einen Fehler: {payload}")
        return payload["result"]

    def _parse_package_as_sitzung(self, pkg: dict) -> Sitzung | None:
        name = pkg.get("name")
        publishing_date = self._extra_date(pkg, "publishing_date")
        if not name or publishing_date is None:
            return None
        return Sitzung(
            source_id=self.source.id,
            external_id=name,
            gremium="Bauleitplanung",
            datum=publishing_date,
            url=f"{self.source.base_url}package_show?id={name}",
        )

    def _parse_resource(self, resource: dict, sitzung_external_id: str) -> Dokument | None:
        if (resource.get("format") or "").strip().upper() != "PDF":
            return None  # GML/WFS/HTML-Metabeschreibung u. Ä. sind keine auswertbaren Dokumente
        url = resource.get("url")
        resource_id = resource.get("id")
        if not url or not resource_id:
            return None
        titel = normalize(resource.get("name") or "") or "(ohne Titel)"
        titel_lower = titel.lower()
        typ = next((t for kw, t in _TYP_KEYWORDS if kw in titel_lower), "anlage")
        return Dokument(
            source_id=self.source.id,
            external_id=resource_id,
            titel=titel,
            url=url,
            sitzung_external_id=sitzung_external_id,
            vorlage_external_id=sitzung_external_id,  # ein Package = eine Sitzung = eine Vorlage
            typ=typ,
        )

    @staticmethod
    def _extra_date(pkg: dict, key: str) -> date | None:
        for extra in pkg.get("extras") or []:
            if extra.get("key") == key:
                try:
                    return datetime.fromisoformat(extra["value"]).date()
                except (TypeError, ValueError):
                    return None
        return None

    def _get_json(self, url: str) -> dict:
        response = self.http.get(url)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceLayoutError(f"{self.source.id}: Antwort von {url} ist kein gültiges JSON ({exc})") from None
