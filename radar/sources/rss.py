"""Adapter für RSS-Feeds lokaler Medien (Phase 6, Quelle B in docs/KONZEPT_PRESSEQUELLEN.md).

Referenz: noa4.de, Rubrik "Lokal" (WordPress-Feed, live geprüft am 20.11.2026, Fixtures `tests/fixtures/noa4/`):
`<item>` mit `title`, `link`, `pubDate`, mehreren `category` (oft auch der Ortsname), `guid` "https://.../?p=<ID>"
und einer `description` mit nur ~160 Zeichen Anriss. 10 Artikel je Seite, ältere Seiten über `?paged=<n>`.

Schritt 1 (dieser Stand): Titel + Anriss + Kategorien werden als `Dokument.text` übernommen (wie bei bv-hh.de,
kein Download) und laufen durch Vorfilter und KI-Auswertung wie Ratsunterlagen. Der volle Artikeltext folgt in
Schritt 2. Urheberrecht: der Text wird nur intern ausgewertet, im Dashboard erscheinen höchstens Titel, Link und
ein kurzes Zitat (`evidenz` ≤ 200 Zeichen).

Je Artikel eine `Sitzung` (Gremium `gremium`, Standard "Presseartikel", Datum = Veröffentlichung in deutscher
Zeit) mit genau einem `Dokument`. Gelistet werden nur Artikel, deren Titel/Anriss/Kategorien zu `titel_muster`
**und** (falls gesetzt) `ort_muster` passen. Fail loud: liefert die erste Feed-Seite keinen einzigen Artikel, ist
das ein Layoutwechsel (`SourceLayoutError`).

Optionen (`options` in config/sources.yaml):
  feed_url      Pfad des Feeds relativ zu `base_url` (Pflicht)
  titel_muster  Regex-Liste (Groß-/Kleinschreibung egal), mindestens eine muss passen; leer = alle
  ort_muster    Regex-Liste, mindestens eine muss passen (z. B. Ortsname); leer = keine Ortsprüfung
  max_seiten    höchstens so viele Feed-Seiten je Lauf (Standard 5 = 50 Artikel); ältere Artikel holt Schritt 3
  gremium       Name für das Feld Gremium (Standard "Presseartikel")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from radar.committees import normalize
from radar.sources.base import Dokument, Sitzung, SourceAdapter, SourceLayoutError

logger = logging.getLogger(__name__)

STANDARD_GREMIUM = "Presseartikel"
_ZEITZONE = ZoneInfo("Europe/Berlin")
_POST_ID = re.compile(r"[?&]p=(\d+)")


def _text(item, name: str) -> str:
    element = item.find(name)
    return element.get_text() if element is not None else ""


@dataclass(frozen=True)
class Artikel:
    artikel_id: str
    titel: str
    url: str
    datum: date
    anriss: str
    kategorien: tuple[str, ...]

    @property
    def text(self) -> str:
        teile = [self.titel, self.anriss]
        if self.kategorien:
            teile.append("Schlagworte: " + ", ".join(self.kategorien))
        return "\n\n".join(t for t in teile if t)


class RssAdapter(SourceAdapter):
    system = "rss"

    def __init__(self, source, http=None):
        super().__init__(source, http)
        opt = source.options or {}
        if not opt.get("feed_url"):
            raise ValueError(f"Quelle '{source.id}': options.feed_url fehlt")
        self._feed_url = opt["feed_url"]
        self._titel_re = [re.compile(m, re.IGNORECASE) for m in opt.get("titel_muster", [])]
        self._ort_re = [re.compile(m, re.IGNORECASE) for m in opt.get("ort_muster", [])]
        self._max_seiten = int(opt.get("max_seiten", 5))
        self._gremium = opt.get("gremium", STANDARD_GREMIUM)
        self._artikel: dict[str, Artikel] = {}
        # Bis wohin der letzte list_sessions()-Aufruf wirklich gelesen hat. Endet das Blättern an `max_seiten`,
        # bevor der Fensterbeginn erreicht ist, ist das Fenster NICHT vollständig - der Scraper vermerkt dann nur
        # dieses Datum als erfasst (`radar.scraper`, `abgedeckt_ab`), damit ein späteres Nachladen nicht entfällt.
        self.abgedeckt_ab: date | None = None

    # -- öffentliche Schnittstelle ----------------------------------------------------------
    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        self.abgedeckt_ab = start
        aeltestes: date | None = None
        for seite in range(1, self._max_seiten + 1):
            url = self._feed_url if seite == 1 else f"{self._feed_url}?paged={seite}"
            try:
                xml = self.http.get(url).text
            except requests.HTTPError:
                if seite == 1:
                    raise
                break  # hinter der letzten Seite liefert WordPress 404 -> Feed vollständig gelesen
            artikel = self.parse_feed(xml, erste_seite=seite == 1)
            if not artikel:
                break
            for a in artikel:
                self._artikel.setdefault(a.artikel_id, a)
            aeltestes = min(a.datum for a in artikel)
            if aeltestes < start:
                break  # Feed ist nach Datum sortiert - ältere Seiten liegen außerhalb des Fensters
        else:
            # max_seiten erreicht, ohne den Fensterbeginn zu sehen: nur bis zum ältesten gelesenen Artikel erfasst
            if aeltestes is not None and aeltestes > start:
                self.abgedeckt_ab = aeltestes
        return [
            Sitzung(source_id=self.source.id, external_id=a.artikel_id, gremium=self._gremium, datum=a.datum, url=a.url)
            for a in self._artikel.values()
            if start <= a.datum <= end and self._passt(a)
        ]

    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        a = self._artikel[sitzung.external_id]
        return [
            Dokument(
                source_id=self.source.id,
                external_id=f"artikel-{a.artikel_id}",
                titel=a.titel,
                url=a.url,
                sitzung_external_id=a.artikel_id,
                vorlage_external_id=f"artikel-{a.artikel_id}",
                vorlage_nr=f"Artikel {a.artikel_id}",
                typ="presseartikel",
                text=a.text,
            )
        ]

    # -- intern -------------------------------------------------------------------------------
    def _passt(self, a: Artikel) -> bool:
        pruef = f"{a.titel} {a.anriss} {' '.join(a.kategorien)}"
        if self._titel_re and not any(m.search(pruef) for m in self._titel_re):
            return False
        return not self._ort_re or any(m.search(pruef) for m in self._ort_re)

    def parse_feed(self, xml: str, *, erste_seite: bool = True) -> list[Artikel]:
        soup = BeautifulSoup(xml, "xml")
        items = soup.find_all("item")
        if not items and erste_seite:
            raise SourceLayoutError(f"{self.source.id}: Feed {self._feed_url} enthält keinen Artikel (Layoutwechsel?)")
        artikel = []
        for item in items:
            link = (_text(item, "link") or "").strip()
            guid = (_text(item, "guid") or "").strip()
            treffer = _POST_ID.search(guid) or _POST_ID.search(link)
            artikel_id = treffer.group(1) if treffer else re.sub(r"\W+", "-", link.rstrip("/").rsplit("/", 1)[-1])
            datum_text = _text(item, "pubDate")
            if not (link and artikel_id and datum_text):
                continue
            anriss_html = _text(item, "description") or ""
            anriss = BeautifulSoup(anriss_html, "lxml").get_text(" ", strip=True)
            anriss = re.sub(r"\s*Source\s*$", "", anriss)  # angehängter "Source"-Link des Feeds
            artikel.append(
                Artikel(
                    artikel_id=artikel_id,
                    titel=normalize(_text(item, "title") or ""),
                    url=link,
                    datum=parsedate_to_datetime(datum_text).astimezone(_ZEITZONE).date(),
                    anriss=normalize(anriss),
                    kategorien=tuple(normalize(c.get_text()) for c in item.find_all("category")),
                )
            )
        return artikel
