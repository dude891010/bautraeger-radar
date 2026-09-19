"""PDF-Textextraktion, Keyword-Vorfilter und LLM-Extraktion (Schritt 3).

Dreistufig, siehe CLAUDE.md "PDF- & LLM-Regeln":
  Stufe 1 (`matched_keywords`, kein LLM)   -> nur Vorlagen mit Treffer gehen weiter.
  Stufe 2 (`triage`, günstiges Modell)     -> grobe Ja/Nein/Unklar-Vorprüfung.
  Stufe 3 (`extract`, starkes Modell)      -> strukturierte Feldextraktion, Pydantic-validiert.

`analyze_documents()` ist der Haupteinstieg: bekommt alle PDFs **einer Vorlage** (mehrere
Dokumente gemeinsam, weil Wohneinheiten oft nur in Anlagen stehen), extrahiert Text, filtert,
triagiert, extrahiert und cacht das Ergebnis pro Dokument-Hash-Kombination.

Netzwerk (Anthropic-Client) ist hinter `client: anthropic.Anthropic` versteckt, Tests ersetzen ihn
durch ein Fake-Objekt mit passendem `.messages.create(...)` – kein Netzzugriff in `pytest`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import anthropic
import pdfplumber
import tenacity
from pydantic import BaseModel, Field, ValidationError

from radar.config import Settings

logger = logging.getLogger(__name__)

_TRANSIENT_ERRORS = (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError)


# -- Domänenmodelle (Pydantic, per Tool-Use erzwungen und validiert) -----------------------------


class Wohnform(StrEnum):
    MIETE = "MIETE"
    GEFOERDERT = "GEFOERDERT"
    EIGENTUM = "EIGENTUM"
    GEMISCHT = "GEMISCHT"
    SONDERWOHNFORM = "SONDERWOHNFORM"
    UNKLAR = "UNKLAR"


class Verfahrensstand(StrEnum):
    VORBERATUNG = "VORBERATUNG"
    AUFSTELLUNGSBESCHLUSS = "AUFSTELLUNGSBESCHLUSS"
    AUSLEGUNG = "AUSLEGUNG"
    SATZUNGSBESCHLUSS = "SATZUNGSBESCHLUSS"
    BAUGENEHMIGUNG = "BAUGENEHMIGUNG"
    UNKLAR = "UNKLAR"


class Adresse(BaseModel):
    strasse: str | None = None
    hausnummer: str | None = None
    flurstueck: str | None = None
    ort: str | None = None


class ProjektExtraktion(BaseModel):
    """Stufe-3-Ergebnis. Felder wie in CLAUDE.md "Fachliche Regeln" > "Felder pro Vorhaben".
    Herkunftsfelder (bundesland, kommune, quelle_id, gremium, sitzungsdatum, Vorlagen-Nr., URL)
    kommen nicht vom LLM, sondern werden vom Aufrufer aus Sitzung/Dokument/Source ergänzt."""

    projektbezeichnung: str | None = None
    adresse: Adresse = Field(default_factory=Adresse)
    we_gesamt: int | None = None
    we_miete: int | None = None
    we_gefoerdert_anzahl: int | None = None
    we_gefoerdert: bool | None = None
    quote_gefoerdert: float | None = Field(default=None, ge=0.0, le=1.0)
    ausfuehrungszeitraum: str | None = None
    antragsteller: str | None = None
    kurzfassung: str | None = None
    wohnform: Wohnform = Wohnform.UNKLAR
    verfahrensstand: Verfahrensstand = Verfahrensstand.UNKLAR
    konfidenz: float = Field(default=0.0, ge=0.0, le=1.0)
    evidenz: str | None = Field(default=None, max_length=200)
    seite: int | None = None

    @property
    def unklar(self) -> bool:
        """Fehlt Anzahl oder Wohnform, gilt filters.unknown_*_policy (Default: flag/"prüfen"),
        siehe CLAUDE.md: unklare Fälle nie stillschweigend verwerfen."""
        keine_zahl = self.we_gesamt is None and self.we_miete is None and self.we_gefoerdert_anzahl is None
        return keine_zahl or self.wohnform is Wohnform.UNKLAR


class TriageEntscheidung(StrEnum):
    JA = "JA"
    NEIN = "NEIN"
    UNKLAR = "UNKLAR"


class TriageErgebnis(BaseModel):
    entscheidung: TriageEntscheidung
    begruendung: str | None = None


class AnalyseStatus(StrEnum):
    RELEVANT = "RELEVANT"              # Extraktion gelaufen, ProjektExtraktion gesetzt
    NICHT_RELEVANT = "NICHT_RELEVANT"  # Vorfilter oder Triage: kein Treffer
    OCR_NOETIG = "OCR_NOETIG"          # kaum Text extrahierbar, keine LLM-Aufrufe gemacht
    BUDGET_ERSCHOEPFT = "BUDGET_ERSCHOEPFT"
    FEHLER = "FEHLER"                  # z. B. Extraktion nach Reparaturversuchen ungültig


@dataclass
class AnalyseErgebnis:
    status: AnalyseStatus
    extraktion: ProjektExtraktion | None = None
    grund: str | None = None
    aus_cache: bool = False


# -- PDF-Textextraktion ---------------------------------------------------------------------------


@dataclass
class ExtrahiertesPdf:
    pages: list[str]
    ocr_noetig: bool

    @property
    def text(self) -> str:
        return "\n\n".join(f"[Seite {i + 1}]\n{page}" for i, page in enumerate(self.pages))


def extract_pdf_text(path: Path) -> ExtrahiertesPdf:
    """Text je Seite mit pdfplumber (MIT-Lizenz; PyMuPDF/AGPL bewusst vermieden). Liefert kaum
    Text zurück (z. B. gescanntes PDF ohne Textlayer), wird `ocr_noetig=True` gesetzt statt zu
    raten – OCR ist noch nicht angebunden."""
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append((page.extract_text() or "").strip())

    total_chars = sum(len(p) for p in pages)
    ocr_noetig = not pages or total_chars < 20 * len(pages)
    return ExtrahiertesPdf(pages=pages, ocr_noetig=ocr_noetig)


@dataclass
class Textinhalt:
    """Bereits extrahierter Text anstelle eines PDF-Pfades - für Quellen ohne eigene PDF-Anlagen,
    die den Dokumenttext selbst schon aufbereitet liefern (z. B. radar/sources/bv_hh.py: HTML-
    Drucksachen ohne separate Datei). `analyze_documents()`/`documents_cache_key()` nehmen sowohl
    `Path` (PDF, wird mit `extract_pdf_text()` gelesen) als auch `Textinhalt` entgegen; für Text
    entfällt die OCR-Frage (`ocr_noetig` ist immer `False`), sonst identische Behandlung."""

    text: str


def _lade_text(quelle: Path | Textinhalt) -> ExtrahiertesPdf:
    if isinstance(quelle, Textinhalt):
        return ExtrahiertesPdf(pages=[quelle.text], ocr_noetig=False)
    return extract_pdf_text(quelle)


# -- Stufe 1: Keyword-Vorfilter -------------------------------------------------------------------


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return [kw for kw in keywords if kw.lower() in lowered]


def _relevant_page_indices(pages: list[str], keywords: list[str], context_pages: int) -> list[int]:
    hits = {i for i, page in enumerate(pages) if matched_keywords(page, keywords)}
    selected: set[int] = set()
    for i in hits:
        selected.update(range(max(0, i - context_pages), min(len(pages), i + context_pages + 1)))
    return sorted(selected)


def build_excerpt(pages: list[str], keywords: list[str], context_pages: int, max_chars: int) -> str:
    """Statt blind abzuschneiden: nur Seiten mit Keyword-Treffer ± Nachbarseiten, und falls das
    immer noch zu lang ist, ganze Seiten vom Ende her weglassen (nie mitten im Text kürzen)."""
    indices = _relevant_page_indices(pages, keywords, context_pages)
    parts: list[str] = []
    total = 0
    for i in indices:
        block = f"[Seite {i + 1}]\n{pages[i]}"
        if total + len(block) > max_chars and parts:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


# -- Prompts ----------------------------------------------------------------------------------------


def _load_prompt(settings: Settings, filename: str) -> str:
    path = settings.resolve_path(settings.llm.prompts_dir) / filename
    return path.read_text(encoding="utf-8")


# -- Kostenkontrolle: Budget + Cache ---------------------------------------------------------------


@dataclass
class LLMBudget:
    """Kostenbremse pro Lauf und pro Quelle (CLAUDE.md: `max_calls_per_run`/`max_calls_per_source`).
    Ein Aufruf reserviert sich Budget *vor* dem API-Call; wird nichts reserviert, wird gar nicht
    erst angefragt."""

    max_calls_per_run: int
    max_calls_per_source: int
    _total: int = 0
    _pro_quelle: dict[str, int] = field(default_factory=dict)

    def reserve(self, quelle_id: str) -> bool:
        if self._total >= self.max_calls_per_run:
            return False
        if self._pro_quelle.get(quelle_id, 0) >= self.max_calls_per_source:
            return False
        self._total += 1
        self._pro_quelle[quelle_id] = self._pro_quelle.get(quelle_id, 0) + 1
        return True


class ResultCache:
    """Extraktionsergebnisse pro Dokument-Hash cachen (Datei-basiert unter `llm.cache_dir`), damit
    ein wiederholter Lauf über unveränderte Dokumente keine erneuten LLM-Aufrufe macht."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, key: str) -> ProjektExtraktion | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return ProjektExtraktion.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, json.JSONDecodeError):
            logger.warning("Cache-Eintrag %s ungültig, wird ignoriert", key)
            return None

    def set(self, key: str, extraktion: ProjektExtraktion) -> None:
        self._path(key).write_text(extraktion.model_dump_json(), encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def documents_cache_key(quellen: list[Path | Textinhalt]) -> str:
    """Stabiler Schlüssel aus den SHA-256-Hashes aller Dokumente einer Vorlage (Reihenfolge
    egal), damit eine geänderte Anlage automatisch einen neuen Cache-Eintrag erzeugt. Für
    `Textinhalt` wird der Text selbst gehasht (kein Dateiinhalt vorhanden)."""

    def _hash(quelle: Path | Textinhalt) -> str:
        if isinstance(quelle, Textinhalt):
            return hashlib.sha256(quelle.text.encode("utf-8")).hexdigest()
        return file_sha256(quelle)

    hashes = sorted(_hash(q) for q in quellen)
    return hashlib.sha256("|".join(hashes).encode("utf-8")).hexdigest()


# -- Anthropic-Aufrufe --------------------------------------------------------------------------
#
# `client.messages.parse(output_format=<Pydantic-Modell>)` ist die native, in dieser SDK-/
# API-Generation (anthropic>=1.7, Claude 5) vorgesehene Art, JSON-Schema-Output zu erzwingen: die
# API generiert direkt anhand des aus dem Pydantic-Modell abgeleiteten Schemas, die SDK validiert
# die Antwort selbst mit demselben Modell (`response.parsed_output`) und wirft bei einer
# ungültigen Antwort ein `pydantic.ValidationError` – das fängt der Reparaturversuch in
# `extract()` ab. Einfacher und robuster als der früher genutzte Tool-Use-Umweg.
#
# **Kein `temperature`-Parameter**: in dieser API-Generation gibt es ihn nicht mehr (geprüft per
# `inspect.signature`), CLAUDE.md's "Temperatur 0" lässt sich mit dem aktuellen SDK nicht mehr
# wörtlich umsetzen. Die strukturierte Ausgabe schränkt die Variation im Ergebnisformat ohnehin
# stark ein; `settings.llm.temperature` bleibt zur Dokumentation der ursprünglichen Absicht in der
# Konfiguration, wird hier aber nicht mehr an die API übergeben.


@tenacity.retry(
    retry=tenacity.retry_if_exception_type(_TRANSIENT_ERRORS),
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _call(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    user_text: str,
    output_format: type[BaseModel],
    max_tokens: int,
) -> tuple[BaseModel, object]:
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        output_format=output_format,
    )
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError(f"Keine strukturierte Antwort für '{output_format.__name__}' erhalten")
    return parsed, response.usage


def _log_usage(quelle_id: str, stufe: str, usage: object) -> None:
    logger.info(
        "LLM-Aufruf Quelle '%s' Stufe %s: %s input_tokens, %s output_tokens",
        quelle_id,
        stufe,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )


def triage(client: anthropic.Anthropic, settings: Settings, text: str, quelle_id: str) -> TriageErgebnis:
    system = _load_prompt(settings, "triage_system.md")
    ergebnis, usage = _call(
        client,
        model=settings.llm.triage_model,
        system=system,
        user_text=f"<dokument>\n{text}\n</dokument>",
        output_format=TriageErgebnis,
        max_tokens=200,
    )
    _log_usage(quelle_id, "Triage", usage)
    return ergebnis


def extract(client: anthropic.Anthropic, settings: Settings, text: str, quelle_id: str) -> ProjektExtraktion | None:
    """Bis zu zwei Versuche: schlägt die Pydantic-Validierung fehl (wirft `client.messages.parse`
    selbst, siehe oben), wird die Fehlermeldung dem Modell zur Reparatur zurückgegeben. Scheitert
    auch das, wird `None` zurückgegeben und der Aufrufer loggt das als Fehler (CLAUDE.md:
    höchstens 1-2 Reparaturversuche, dann als "Fehler")."""
    system = _load_prompt(settings, "extraction_system.md")
    user_text = f"<dokument>\n{text}\n</dokument>"

    last_error: str | None = None
    for versuch in range(1, 3):
        prompt = user_text
        if last_error is not None:
            prompt += (
                f"\n\n<fehler>\nDeine letzte Antwort war ungültig: {last_error}\n"
                "Bitte korrigiere und antworte erneut.\n</fehler>"
            )
        try:
            ergebnis, usage = _call(
                client,
                model=settings.llm.extraction_model,
                system=system,
                user_text=prompt,
                output_format=ProjektExtraktion,
                max_tokens=settings.llm.max_tokens,
            )
        except ValidationError as exc:
            last_error = str(exc)
            logger.warning("Extraktion für Quelle '%s' ungültig (Versuch %d): %s", quelle_id, versuch, exc)
            continue
        _log_usage(quelle_id, f"Extraktion (Versuch {versuch})", usage)
        return ergebnis

    logger.error("Extraktion für Quelle '%s' nach Reparaturversuchen weiterhin ungültig: %s", quelle_id, last_error)
    return None


# -- Haupteinstieg ------------------------------------------------------------------------------


def analyze_documents(
    quellen: list[Path | Textinhalt],
    *,
    client: anthropic.Anthropic,
    settings: Settings,
    budget: LLMBudget,
    cache: ResultCache,
    quelle_id: str,
) -> AnalyseErgebnis:
    """Alle Dokumente **einer Vorlage** gemeinsam auswerten (Wohneinheiten stehen oft nur in
    Anlagen). Jedes Element ist entweder ein PDF-`Path` (wird mit `extract_pdf_text()` gelesen)
    oder ein `Textinhalt` (Text schon vorhanden, z. B. von radar/sources/bv_hh.py). Reihenfolge:
    Cache -> Text -> Stufe 1 (Keyword) -> Stufe 2 (Triage) -> Stufe 3 (Extraktion). Ein NEIN aus
    der Triage beendet die Auswertung (spart die teure Stufe 3); JA und UNKLAR gehen beide weiter,
    damit unklare Fälle nie an dieser Stelle stillschweigend verworfen werden."""
    if not quellen:
        return AnalyseErgebnis(status=AnalyseStatus.NICHT_RELEVANT, grund="keine Dokumente")

    cache_key = documents_cache_key(quellen)
    cached = cache.get(cache_key)
    if cached is not None:
        return AnalyseErgebnis(status=AnalyseStatus.RELEVANT, extraktion=cached, aus_cache=True)

    try:
        extrahiert = [_lade_text(q) for q in quellen]
    except Exception as exc:  # z. B. beschädigtes PDF -> nie den Lauf stoppen
        logger.exception("Textextraktion für Quelle '%s' fehlgeschlagen (%s)", quelle_id, quellen)
        return AnalyseErgebnis(status=AnalyseStatus.FEHLER, grund=f"Textextraktion fehlgeschlagen: {exc}")
    nutzbare = [e for e in extrahiert if not e.ocr_noetig]
    if not nutzbare:
        return AnalyseErgebnis(status=AnalyseStatus.OCR_NOETIG, grund=f"{len(quellen)} Dokument(e) ohne Textlayer")

    alle_seiten: list[str] = [seite for e in nutzbare for seite in e.pages]
    keywords = settings.filters.prefilter_keywords
    if not matched_keywords("\n".join(alle_seiten), keywords):
        return AnalyseErgebnis(status=AnalyseStatus.NICHT_RELEVANT, grund="kein Keyword-Treffer (Stufe 1)")

    excerpt = build_excerpt(alle_seiten, keywords, settings.llm.context_pages, settings.llm.max_input_chars)

    if not budget.reserve(quelle_id):
        return AnalyseErgebnis(status=AnalyseStatus.BUDGET_ERSCHOEPFT, grund="LLM-Budget erschöpft (Triage)")
    try:
        triage_ergebnis = triage(client, settings, excerpt, quelle_id)
    except Exception as exc:  # API-Fehler nach Retries, unerwartete Antwort etc. -> nie den Lauf stoppen
        logger.exception("Triage für Quelle '%s' fehlgeschlagen", quelle_id)
        return AnalyseErgebnis(status=AnalyseStatus.FEHLER, grund=f"Triage fehlgeschlagen: {exc}")
    if triage_ergebnis.entscheidung is TriageEntscheidung.NEIN:
        return AnalyseErgebnis(
            status=AnalyseStatus.NICHT_RELEVANT, grund=f"Triage NEIN: {triage_ergebnis.begruendung or ''}".strip()
        )

    if not budget.reserve(quelle_id):
        return AnalyseErgebnis(status=AnalyseStatus.BUDGET_ERSCHOEPFT, grund="LLM-Budget erschöpft (Extraktion)")
    try:
        extraktion = extract(client, settings, excerpt, quelle_id)
    except Exception as exc:  # API-Fehler nach Retries, unerwartete Antwort etc. -> nie den Lauf stoppen
        logger.exception("Extraktion für Quelle '%s' fehlgeschlagen", quelle_id)
        return AnalyseErgebnis(status=AnalyseStatus.FEHLER, grund=f"Extraktion fehlgeschlagen: {exc}")
    if extraktion is None:
        return AnalyseErgebnis(status=AnalyseStatus.FEHLER, grund="Extraktion nach Reparaturversuchen ungültig")

    cache.set(cache_key, extraktion)
    return AnalyseErgebnis(status=AnalyseStatus.RELEVANT, extraktion=extraktion)
