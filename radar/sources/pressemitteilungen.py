"""Adapter für Pressemitteilungs-Listen einer Kommune (Phase 6, docs/KONZEPT_PRESSEQUELLEN.md).

Referenz: Stadt Norderstedt, Seite "Pressestelle" (live geprüft am 20.11.2026, Fixture
`tests/fixtures/norderstedt_presse/pressestelle.html`): eine einzige Seite mit allen Pressemitteilungen als PDF-Links
`/media/custom/3224_<ID>_1.PDF?<Unix-Zeitstempel>`, Linktext = Titel, neueste zuerst, manche Meldungen doppelt.

Abbildung auf die gemeinsamen Typen, damit Download, Text-Cache, Vorfilter, KI-Auswertung, Datenbank, Dashboard und
Recall unverändert funktionieren: je Pressemitteilung eine `Sitzung` (Gremium `gremium`, Standard "Pressemitteilung",
Datum aus dem Zeitstempel im Link - vermutlich Hochlade-/Änderungszeit, als Näherung ausreichend) mit genau einem
`Dokument` (dem PDF), die PM-ID als `vorlage_external_id`, damit es ausgewertet wird.

Nur Meldungen, deren **Titel** zu `titel_muster` passt (Regex-Liste in `config/sources.yaml`), werden überhaupt
gelistet - so werden von ~740 Meldungen nur die Wohnbau-Themen heruntergeladen. Fail loud: findet die Seite keinen
einzigen PDF-Link, ist das ein Layoutwechsel (`SourceLayoutError`), kein "nichts Neues".

Optionen (`options` in config/sources.yaml):
  list_page     Pfad der Listenseite relativ zu `base_url` (Pflicht)
  link_muster   Regex für den PDF-Link mit Gruppen `id` und `ts` (Standard: Norderstedter Muster)
  titel_muster  Liste von Regexen (Groß-/Kleinschreibung egal); leer = alle Meldungen
  gremium       Name für das Feld Gremium (Standard "Pressemitteilung")
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from radar.committees import normalize
from radar.sources.base import Dokument, Sitzung, SourceAdapter, SourceLayoutError

logger = logging.getLogger(__name__)

STANDARD_LINK_MUSTER = r"/media/custom/\d+_(?P<id>\d+)_1\.PDF\?(?P<ts>\d{9,11})"
STANDARD_GREMIUM = "Pressemitteilung"
_ZEITZONE = ZoneInfo("Europe/Berlin")


class PressemitteilungenAdapter(SourceAdapter):
    system = "pressemitteilungen"

    def __init__(self, source, http=None):
        super().__init__(source, http)
        opt = source.options or {}
        if not opt.get("list_page"):
            raise ValueError(f"Quelle '{source.id}': options.list_page fehlt")
        self._list_page = opt["list_page"]
        self._link_re = re.compile(opt.get("link_muster", STANDARD_LINK_MUSTER))
        self._titel_re = [re.compile(m, re.IGNORECASE) for m in opt.get("titel_muster", [])]
        self._gremium = opt.get("gremium", STANDARD_GREMIUM)
        self._meldungen: dict[str, tuple[str, str, date]] | None = None  # id -> (titel, url, datum)

    # -- öffentliche Schnittstelle ----------------------------------------------------------
    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        return [
            Sitzung(source_id=self.source.id, external_id=pm_id, gremium=self._gremium, datum=datum, url=url)
            for pm_id, (_, url, datum) in self._lade_liste().items()
            if start <= datum <= end
        ]

    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        titel, url, _ = self._lade_liste()[sitzung.external_id]
        return [
            Dokument(
                source_id=self.source.id,
                external_id=f"pm-{sitzung.external_id}",
                titel=titel,
                url=url,
                sitzung_external_id=sitzung.external_id,
                vorlage_external_id=f"pm-{sitzung.external_id}",
                vorlage_nr=f"PM {sitzung.external_id}",
                typ="pressemitteilung",
            )
        ]

    # -- intern -------------------------------------------------------------------------------
    def _lade_liste(self) -> dict[str, tuple[str, str, date]]:
        """Listenseite einmal je Lauf abrufen und parsen (alle Meldungen stehen auf einer Seite)."""
        if self._meldungen is None:
            response = self.http.get(self._list_page)
            self._meldungen = self.parse_liste(response.text)
        return self._meldungen

    def parse_liste(self, html_text: str) -> dict[str, tuple[str, str, date]]:
        soup = BeautifulSoup(html_text, "lxml")
        meldungen: dict[str, tuple[str, str, date]] = {}
        alle = 0
        for link in soup.find_all("a", href=True):
            treffer = self._link_re.search(link["href"])
            if treffer is None:
                continue
            alle += 1
            pm_id = treffer.group("id")
            titel = normalize(link.get_text(" ", strip=True))
            if pm_id in meldungen or not titel:
                continue  # doppelt gelistete Meldung oder Link ohne Text (z. B. Vorschaubild)
            if self._titel_re and not any(m.search(titel) for m in self._titel_re):
                continue
            datum = datetime.fromtimestamp(int(treffer.group("ts")), _ZEITZONE).date()
            meldungen[pm_id] = (titel, link["href"], datum)
        if alle == 0:
            raise SourceLayoutError(
                f"{self.source.id}: keine Pressemitteilungs-Links auf {self._list_page} gefunden "
                "(Layoutwechsel? link_muster prüfen)"
            )
        logger.info("%s: %d Pressemitteilungs-Links, %d mit passendem Titel", self.source.id, alle, len(meldungen))
        return meldungen
