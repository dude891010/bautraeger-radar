"""Gemeinsame Schnittstelle für alle Ratsinformationssysteme.

Jedes System (SessionNet, ALLRIS, OParl, ...) bekommt einen Adapter, der dieselben
Datentypen liefert. Alles danach (Parser, Datenbank, Dashboard) kennt nur diese Typen.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from radar.config import Source


@dataclass(frozen=True)
class Sitzung:
    source_id: str
    external_id: str            # ID im Fremdsystem, z. B. SessionNet __ksinr
    gremium: str
    datum: date
    uhrzeit: str | None = None
    ort: str | None = None
    url: str | None = None      # None = keine Detailseite/Unterlagen (Normalfall, später erneut prüfen)
    abgesagt: bool = False


@dataclass(frozen=True)
class Dokument:
    source_id: str
    external_id: str            # z. B. SessionNet getfile.php?id=...
    titel: str
    url: str                    # bei `text` gesetzt: nur Referenz/Anzeige, wird nicht abgerufen
    sitzung_external_id: str
    vorlage_external_id: str | None = None
    vorlage_nr: str | None = None  # menschlich lesbare Vorlagen-Nr., z. B. "A 26/0338" (falls bekannt)
    typ: str = "unbekannt"      # vorlage | anlage | niederschrift | bekanntmachung | unbekannt
    # Gesetzt, wenn die Quelle den Text bereits selbst extrahiert liefert und kein eigenes PDF zum
    # Herunterladen hat (z. B. radar/sources/bv_hh.py): `url` wird dann nicht abgerufen, `text`
    # geht direkt in radar.parser.analyze_documents() (siehe dort: Path | Textinhalt).
    text: str | None = None


class SourceLayoutError(RuntimeError):
    """Die Seite entspricht nicht dem erwarteten Systemtyp/Layout (Fingerprint- oder
    Struktur-Check fehlgeschlagen). Fail loud statt still 'nichts gefunden' zu melden
    (siehe CLAUDE.md, Abschnitt "Scraping-Regeln")."""


class SourceAdapter(ABC):
    """Basisklasse. `http` ist der gemeinsame HTTP-Client (Session, Retry, Rate-Limit),
    der in Schritt 2 entsteht. Adapter enthalten nur Systemlogik, keine Netzwerkdetails."""

    system: str = ""

    def __init__(self, source: Source, http=None):
        if source.system != self.system:
            raise ValueError(
                f"{type(self).__name__} ist für '{self.system}', Quelle '{source.id}' nutzt '{source.system}'"
            )
        self.source = source
        self.http = http

    @abstractmethod
    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        """Alle Sitzungen (aller Gremien) im Zeitraum. Filter auf Ausschüsse passiert danach."""

    @abstractmethod
    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        """Vorlagen und Anlagen einer Sitzung. Leere Liste, wenn (noch) keine Unterlagen öffentlich sind."""
