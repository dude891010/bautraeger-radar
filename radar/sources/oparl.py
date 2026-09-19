"""Adapter für OParl (offener, JSON-basierter Standard für Ratsinformationssysteme).

Bevorzugter Weg, wo eine Kommune ihn anbietet (CLAUDE.md: "OParl bevorzugen"): strukturierte
Daten statt HTML-Scraping, ein Adapter für mehrere Systemhersteller (SD.NET RIM, ALLRIS-Erweiterung,
more-rubin, ...). Ablauf: feste System-URL (JSON) -> Body (i. d. R. eine Kommune) -> Meeting
(Sitzung, als Liste zunächst nur ein Stub ohne Tagesordnung) -> pro Sitzung Detailabruf mit
`agendaItem` -> je Tagesordnungspunkt optional `consultation` -> `paper` (Vorlage) mit `mainFile`
und `auxiliaryFile` (Anlagen). Alle Listen sind paginiert (`links.next`).

Nicht jede OParl-API liefert Dateien mit (manche nur Metadaten) - `downloadUrl`/`accessUrl` also
immer prüfen, bevor ein Dokument übernommen wird.

Verifiziert am 19.09.2026 gegen die echte, öffentliche OParl-1.1-API der Gemeinde Uplengen
(Niedersachsen, STERNBERG SD.NET RIM, https://uplengen.ratsinfomanagement.net/webservice/oparl/v1.1/):
System/Body/Meeting-Liste, Meeting-Detail mit Tagesordnung, Consultation und Paper wurden live
abgerufen und die Struktur gegen echte Antworten geprüft (siehe tests/fixtures/uplengen/).
PDF-Downloads dieser Quelle wurden dabei bewusst NICHT abgerufen: ihre robots.txt sperrt
`Disallow: /*.pdf$` - genau das Wildcard-Muster, das `urllib.robotparser` nicht versteht und das
zur Ablösung durch `protego` in `radar/http_client.py` geführt hat (siehe dort). Der eigentliche
Download läuft über den bereits an Norderstedt live verifizierten, gemeinsamen
`HttpClient.download_pdf()`; hier nur mit synthetischen PDF-Bytes getestet (tests/test_oparl.py).
Weil dort jede Vorlage ausschließlich als PDF vorliegt, wäre Uplengen ohne PDF-Zugriff für den
echten Betrieb wertlos - in config/sources.yaml deshalb `status: verified`, aber `enabled: false`.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from radar.committees import normalize
from radar.sources.base import Dokument, Sitzung, SourceAdapter, SourceLayoutError

logger = logging.getLogger(__name__)

# Sitzungsname folgt bei den bisher geprüften Herstellern dem Muster "<Gremium> (<N>. Sitzung)".
_SESSION_SUFFIX_RE = re.compile(r"^(.*?)\s*\(\d+\.\s*Sitzung\)\s*$")


class OParlAdapter(SourceAdapter):
    system = "oparl"

    # -- öffentliche Schnittstelle ------------------------------------------------------------
    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        system = self._get_json(self.source.base_url)
        self._verify_fingerprint(system)
        body = self._select_body(system)

        meeting_list_url = body.get("meeting")
        if not meeting_list_url:
            raise SourceLayoutError(f"{self.source.id}: Body ohne 'meeting'-Endpunkt (OParl-Layoutwechsel?)")

        sessions: list[Sitzung] = []
        url: str | None = meeting_list_url
        while url:
            page = self._get_json(url)
            for raw in page.get("data") or []:
                sitzung = self._parse_meeting_stub(raw)
                if sitzung is not None and start <= sitzung.datum <= end:
                    sessions.append(sitzung)
            url = (page.get("links") or {}).get("next")
        return sessions

    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        if sitzung.url is None:
            return []  # kein Detail-Endpunkt bekannt (sollte bei OParl nicht vorkommen)
        meeting = self._get_json(sitzung.url)

        documents: list[Dokument] = []
        for feld, typ in (("invitation", "einladung"), ("resultsProtocol", "niederschrift")):
            raw_file = meeting.get(feld)
            if raw_file is not None:
                doc = self._parse_file(raw_file, sitzung.external_id, None, None, typ)
                if doc is not None:
                    documents.append(doc)
        for raw_file in meeting.get("auxiliaryFile") or []:
            doc = self._parse_file(raw_file, sitzung.external_id, None, None, "anlage")
            if doc is not None:
                documents.append(doc)

        for item in meeting.get("agendaItem") or []:
            if item.get("public") is False:
                continue  # nichtöffentlich -> nicht erfassen (CLAUDE.md: nur öffentliche Unterlagen)
            documents.extend(self._documents_for_agenda_item(item, sitzung.external_id))

        return documents

    # -- intern ---------------------------------------------------------------------------------
    def _documents_for_agenda_item(self, item: dict, sitzung_external_id: str) -> list[Dokument]:
        vorlage_id: str | None = None
        vorlage_nr: str | None = None
        docs: list[Dokument] = []

        consultation_url = item.get("consultation")
        if consultation_url:
            consultation = self._get_json(consultation_url)
            paper_url = consultation.get("paper")
            if paper_url:
                paper = self._get_json(paper_url)
                vorlage_id = paper.get("id")
                vorlage_nr = paper.get("reference")
                main_file = paper.get("mainFile")
                if main_file is not None:
                    doc = self._parse_file(main_file, sitzung_external_id, vorlage_id, vorlage_nr, "vorlage")
                    if doc is not None:
                        docs.append(doc)
                for raw_file in paper.get("auxiliaryFile") or []:
                    doc = self._parse_file(raw_file, sitzung_external_id, vorlage_id, vorlage_nr, "anlage")
                    if doc is not None:
                        docs.append(doc)

        if vorlage_id is None:
            # Kein Paper referenziert (z. B. ein Verfahrenspunkt mit eigenem Beschluss, aber ohne
            # Vorlage) -> eigene ID aus dem Tagesordnungspunkt, ohne Bezug über Sitzungen hinweg.
            vorlage_id = item.get("id")
            vorlage_nr = item.get("number")

        resolution_file = item.get("resolutionFile")
        if resolution_file is not None:
            doc = self._parse_file(resolution_file, sitzung_external_id, vorlage_id, vorlage_nr, "beschluss")
            if doc is not None:
                docs.append(doc)
        for raw_file in item.get("auxiliaryFile") or []:
            doc = self._parse_file(raw_file, sitzung_external_id, vorlage_id, vorlage_nr, "anlage")
            if doc is not None:
                docs.append(doc)

        return docs

    def _parse_file(
        self, raw: dict, sitzung_external_id: str, vorlage_id: str | None, vorlage_nr: str | None, typ: str
    ) -> Dokument | None:
        url = raw.get("downloadUrl") or raw.get("accessUrl")
        file_id = raw.get("id")
        if not url or not file_id:
            return None  # Metadaten ohne abrufbare Datei (CLAUDE.md: nicht jede OParl-API liefert Dateien mit)
        titel = normalize(raw.get("name") or "") or "(ohne Titel)"
        return Dokument(
            source_id=self.source.id,
            external_id=file_id,
            titel=titel,
            url=url,
            sitzung_external_id=sitzung_external_id,
            vorlage_external_id=vorlage_id,
            vorlage_nr=vorlage_nr,
            typ=typ,
        )

    def _parse_meeting_stub(self, raw: dict) -> Sitzung | None:
        meeting_id = raw.get("id")
        start_raw = raw.get("start")
        if not meeting_id or not start_raw:
            return None
        start_dt = datetime.fromisoformat(start_raw)
        name = normalize(raw.get("name") or "")
        match = _SESSION_SUFFIX_RE.match(name)
        gremium = match.group(1) if match else name
        if not gremium:
            return None

        ort = None
        location = raw.get("location")
        if isinstance(location, dict):
            ort = normalize(location.get("room") or location.get("description") or "") or None

        return Sitzung(
            source_id=self.source.id,
            external_id=meeting_id,  # OParl-IDs sind selbst URLs, stabil und eindeutig je System
            gremium=gremium,
            datum=start_dt.date(),
            uhrzeit=start_dt.strftime("%H:%M"),
            ort=ort,
            url=meeting_id,  # dieselbe URL liefert im Detailabruf die volle Tagesordnung
            abgesagt=bool(raw.get("cancelled", False)),
        )

    def _select_body(self, system: dict) -> dict:
        body_list_url = system.get("body")
        if not body_list_url:
            raise SourceLayoutError(f"{self.source.id}: System-Objekt ohne 'body'-Endpunkt")
        page = self._get_json(body_list_url)
        if (page.get("pagination") or {}).get("totalPages", 1) > 1:
            raise SourceLayoutError(
                f"{self.source.id}: mehr Bodies als eine Seite ({body_list_url}) - Adapter unterstützt "
                "das noch nicht, bitte erst klären, welche Kommune gemeint ist"
            )
        bodies = page.get("data") or []
        if not bodies:
            raise SourceLayoutError(f"{self.source.id}: keine Body (Kommune) unter {body_list_url} gefunden")
        if len(bodies) == 1:
            return bodies[0]

        index = self.source.options.get("body_index")
        if index is None:
            namen = ", ".join(f"{i}={b.get('name')}" for i, b in enumerate(bodies))
            raise SourceLayoutError(
                f"{self.source.id}: mehrere Bodies gefunden ({namen}) - 'body_index' in "
                "sources.yaml (options) setzen, um eindeutig auszuwählen"
            )
        try:
            return bodies[index]
        except (IndexError, TypeError):
            raise SourceLayoutError(
                f"{self.source.id}: body_index={index!r} außerhalb des gültigen Bereichs (0..{len(bodies) - 1})"
            ) from None

    def _verify_fingerprint(self, system: dict) -> None:
        oparl_version = system.get("oparlVersion", "")
        if "schema.oparl.org" not in oparl_version:
            raise SourceLayoutError(
                f"{self.source.id}: Antwort der System-URL sieht nicht nach OParl aus "
                f"(oparlVersion={oparl_version!r}) - Layoutwechsel oder falsches System?"
            )

    def _get_json(self, url: str) -> dict:
        response = self.http.get(url)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceLayoutError(f"{self.source.id}: Antwort von {url} ist kein gültiges JSON ({exc})") from None
