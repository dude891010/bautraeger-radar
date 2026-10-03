"""Orchestrierung des Abrufs (Schritt 2) und des vollständigen Laufs (Schritt 4: + Parser + DB).

Ablauf pro Lauf: aktive Quellen laden (Tier A zuerst) -> je Quelle Adapter holen -> Sitzungen im
Zeitraum listen -> auf relevante Ausschüsse filtern (radar.committees) -> Unterlagen listen ->
PDFs in den Cache laden. Fehler einer Quelle stoppen nie den Lauf, sie werden pro Quelle
protokolliert (Quellen-Monitoring). Pro Host strikt seriell und mit Pause (siehe HttpClient).

Zwei Einstiegspunkte:
  `run()`/`run_source()`/`_collect()`            – nur Scraping, kein Netzzugriff zu Anthropic,
                                                     keine Datenbank. Für Trockenläufe/Tests.
  `run_full()`/`run_source_full()`/`_collect_and_persist()` – vollständiger Lauf: speichert
                                                     Sitzungen/Vorlagen/Dokumente, lässt jede noch
                                                     nicht abgeschlossen analysierte Vorlage durch
                                                     `radar.parser.analyze_documents` laufen und
                                                     schreibt das Ergebnis in die Datenbank
                                                     (`radar.database`). Ein `LLMBudget` gilt über
                                                     den ganzen Lauf (`llm.max_calls_per_run`).
"""

from __future__ import annotations

import calendar
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import anthropic

from radar import database
from radar.committees import committee_matches
from radar.config import Settings, Source, enabled_sources, load_settings, load_sources
from radar.http_client import HttpClient
from radar.notifier import NeuerTreffer
from radar.parser import (
    AnalyseStatus,
    LLMBudget,
    ResultCache,
    Textinhalt,
    analyze_documents,
    documents_cache_key,
    file_sha256,
    make_client,
)
from radar.sources.base import Dokument, Sitzung, SourceAdapter
from radar.sources.registry import get_adapter

logger = logging.getLogger(__name__)


@dataclass
class SourceRunResult:
    """Zustand einer Quelle nach einem Lauf, siehe CLAUDE.md "Quellen-Monitoring"."""

    source_id: str
    sitzungen: int = 0
    relevante_sitzungen: int = 0
    vorlagen: int = 0
    bereits_analysiert: int = 0  # Vorlagen, deren Dokument-Hash unverändert und abgeschlossen analysiert war
    dokumente: int = 0
    heruntergeladen: int = 0
    text_dokumente: int = 0  # Dokumente ohne Download übernommen (Dokument.text), siehe radar/sources/bv_hh.py
    treffer: int = 0  # Vorlagen mit AnalyseStatus.RELEVANT
    neue_treffer: list[NeuerTreffer] = field(default_factory=list)  # nur echte Neuanlagen, für radar/notifier.py
    llm_anfragen: int = 0  # tatsächlich gesendete API-Anfragen dieser Quelle (inkl. Retries)
    ohne_llm: int = 0  # Vorlagen, die wegen Budget/Notbremse nicht analysiert wurden (nächster Lauf versucht es)
    fehler: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fehler


class LaufLaeuftBereits(RuntimeError):
    """Ein anderer vollständiger Lauf (Dashboard-Knopf oder Scheduler) ist noch aktiv."""


# Eine Sperrdatei, die älter ist, gilt als Überbleibsel eines abgestürzten Laufs. Großzügig
# bemessen: ein echter Lauf über viele Quellen mit 2 s Pause pro Request dauert Stunden.
_SPERRE_MAX_ALTER_SEKUNDEN = 12 * 3600


def lauf_sperre_pfad(settings: Settings) -> Path:
    return settings.resolve_path(settings.database.path).parent / "lauf.lock"


def lauf_sperre_aktiv(settings: Settings) -> bool:
    pfad = lauf_sperre_pfad(settings)
    try:
        return time.time() - pfad.stat().st_mtime < _SPERRE_MAX_ALTER_SEKUNDEN
    except FileNotFoundError:
        return False


@contextmanager
def lauf_sperre(settings: Settings) -> Iterator[None]:
    """Höchstens ein vollständiger Lauf gleichzeitig. Ohne Sperre hätten Dashboard-Knopf und
    Scheduler (oder zwei Browser-Tabs) parallel laufen können - jeder mit seinem eigenen, vollen
    `LLMBudget`, also dem Mehrfachen des konfigurierten Höchstwerts. Prozessübergreifend über eine
    exklusiv angelegte Datei neben der Datenbank (`data/lauf.lock`)."""
    pfad = lauf_sperre_pfad(settings)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    if lauf_sperre_aktiv(settings):
        raise LaufLaeuftBereits(f"Ein anderer Lauf ist noch aktiv (Sperrdatei {pfad}: {_lies(pfad)})")
    pfad.unlink(missing_ok=True)  # veraltete Sperre eines abgestürzten Laufs
    try:
        fd = os.open(pfad, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:  # zeitgleich von einem anderen Prozess angelegt
        raise LaufLaeuftBereits(f"Ein anderer Lauf ist noch aktiv (Sperrdatei {pfad})") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"pid={os.getpid()} start={datetime.now().isoformat(timespec='seconds')}\n")
    try:
        yield
    finally:
        pfad.unlink(missing_ok=True)


def _lies(pfad: Path) -> str:
    try:
        return pfad.read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def _shift_months(d: date, months: int) -> date:
    """`d` um `months` Kalendermonate verschieben (negativ = zurück), Tag ggf. auf das
    Monatsende gekappt (z. B. 31. Januar - 1 Monat -> 28./29. Februar)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def scan_range(settings: Settings, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    start = _shift_months(today, -settings.scan.months_back)
    end = _shift_months(today, settings.scan.months_ahead)
    return start, end


def _collect(
    adapter: SourceAdapter,
    http: HttpClient,
    committee_patterns: list[str],
    start: date,
    end: date,
    result: SourceRunResult,
) -> None:
    """Sitzungen einer Quelle abrufen, auf relevante Ausschüsse filtern, Unterlagen laden und
    deren PDFs herunterladen. Ergebnis wird in `result` akkumuliert (auch bei Teilfehlern)."""
    sessions: list[Sitzung] = adapter.list_sessions(start, end)
    result.sitzungen = len(sessions)

    vorlagen_ids: set[str] = set()
    for sitzung in sessions:
        if sitzung.abgesagt or not committee_matches(sitzung.gremium, committee_patterns):
            continue
        result.relevante_sitzungen += 1

        try:
            documents: list[Dokument] = adapter.list_documents(sitzung)
        except Exception as exc:  # eine kaputte Sitzung darf die Quelle nicht abbrechen
            result.fehler.append(f"Unterlagen für Sitzung {sitzung.external_id}: {exc}")
            continue

        vorlagen_ids.update(d.vorlage_external_id for d in documents if d.vorlage_external_id)
        result.dokumente += len(documents)

        for dokument in documents:
            if dokument.text is not None:
                result.text_dokumente += 1
                continue
            try:
                http.download_pdf(dokument.url, dokument.external_id)
                result.heruntergeladen += 1
            except Exception as exc:  # ein kaputter Download darf die Quelle nicht abbrechen
                result.fehler.append(f"Download {dokument.external_id} ({dokument.titel}): {exc}")

    result.vorlagen = len(vorlagen_ids)


def run_source(source: Source, settings: Settings, start: date, end: date) -> SourceRunResult:
    """Eine Quelle abrufen. Fehler werden gefangen und im Ergebnis protokolliert, nie
    weitergereicht (siehe CLAUDE.md: "Quellen sind voneinander isoliert")."""
    result = SourceRunResult(source_id=source.id)
    try:
        http = HttpClient(source, settings)
        adapter = get_adapter(source, http)
        _collect(adapter, http, settings.committee_patterns_for(source), start, end, result)
    except Exception as exc:
        result.fehler.append(str(exc))
        logger.exception("Quelle '%s' fehlgeschlagen", source.id)
    return result


def run(settings: Settings | None = None) -> list[SourceRunResult]:
    """Alle aktivierten Quellen abrufen (Tier A zuerst), Ergebnis je Quelle zurückgeben."""
    settings = settings or load_settings()
    sources = enabled_sources(load_sources(settings), settings)
    start, end = scan_range(settings)

    results = []
    for source in sources:
        logger.info("Quelle '%s' (%s): Zeitraum %s bis %s", source.id, source.name, start, end)
        result = run_source(source, settings, start, end)
        results.append(result)
        logger.info(
            "Quelle '%s': %d Sitzungen, %d relevant, %d Vorlagen, %d Dokumente, %d Downloads, %d Fehler",
            result.source_id,
            result.sitzungen,
            result.relevante_sitzungen,
            result.vorlagen,
            result.dokumente,
            result.heruntergeladen,
            len(result.fehler),
        )
        for fehler in result.fehler:
            logger.warning("Quelle '%s': %s", result.source_id, fehler)

    return results


# -- Vollständiger Lauf: Scraper -> Parser -> Datenbank (Schritt 4) -------------------------------


def _collect_and_persist(
    adapter: SourceAdapter,
    http: HttpClient,
    conn: sqlite3.Connection,
    client: anthropic.Anthropic,
    budget: LLMBudget,
    cache: ResultCache,
    source: Source,
    settings: Settings,
    start: date,
    end: date,
    result: SourceRunResult,
) -> None:
    """Wie `_collect()`, speichert zusätzlich Sitzungen/Vorlagen/Dokumente in der Datenbank und
    lässt jede noch nicht abgeschlossen analysierte Vorlage (siehe `database.upsert_vorlage`)
    durch `radar.parser.analyze_documents` laufen. Alle Dokumente einer Vorlage werden gemeinsam
    ausgewertet (CLAUDE.md: Wohneinheiten stehen oft nur in Anlagen); Dokumente ohne Vorlagen-Bezug
    (Einladung, Bekanntmachung, Niederschrift) werden gespeichert, aber nicht analysiert."""
    sessions: list[Sitzung] = adapter.list_sessions(start, end)
    result.sitzungen = len(sessions)
    committee_patterns = settings.committee_patterns_for(source)

    vorlagen_ids: set[str] = set()
    for sitzung in sessions:
        if sitzung.abgesagt or not committee_matches(sitzung.gremium, committee_patterns):
            continue
        result.relevante_sitzungen += 1
        sitzung_id = database.upsert_sitzung(conn, source.id, sitzung)

        try:
            documents: list[Dokument] = adapter.list_documents(sitzung)
        except Exception as exc:  # eine kaputte Sitzung darf die Quelle nicht abbrechen
            result.fehler.append(f"Unterlagen für Sitzung {sitzung.external_id}: {exc}")
            continue

        result.dokumente += len(documents)

        gruppen: dict[str | None, list[Dokument]] = {}
        for dokument in documents:
            gruppen.setdefault(dokument.vorlage_external_id, []).append(dokument)

        for vorlage_external_id, gruppe in gruppen.items():
            geladen: list[tuple[Dokument, Path | Textinhalt]] = []
            for dokument in gruppe:
                if dokument.text is not None:
                    # Quelle liefert den Text schon selbst (kein eigenes PDF, siehe
                    # radar/sources/bv_hh.py) -> kein Download, `url` wird nicht abgerufen.
                    geladen.append((dokument, Textinhalt(text=dokument.text)))
                    result.text_dokumente += 1
                    continue
                try:
                    pfad = http.download_pdf(dokument.url, dokument.external_id)
                    result.heruntergeladen += 1
                except Exception as exc:  # ein kaputter Download darf die Quelle nicht abbrechen
                    result.fehler.append(f"Download {dokument.external_id} ({dokument.titel}): {exc}")
                    continue
                geladen.append((dokument, pfad))

            if vorlage_external_id is None:
                # Sitzungsweite Dokumente (Einladung/Bekanntmachung/Niederschrift) gehören zu
                # keiner Vorlage -> speichern, aber nicht analysieren.
                for dokument, quelle in geladen:
                    dok_id = database.upsert_dokument(conn, source.id, sitzung_id, None, dokument)
                    if isinstance(quelle, Path):
                        database.mark_dokument_heruntergeladen(conn, dok_id, file_sha256(quelle), str(quelle))
                continue

            if not geladen:
                continue  # alle Downloads dieser Vorlage sind fehlgeschlagen, nichts zu analysieren

            vorlagen_ids.add(vorlage_external_id)
            dokument_quellen = [quelle for _, quelle in geladen]
            vorlagen_nr = next((d.vorlage_nr for d, _ in geladen if d.vorlage_nr), None)
            dokument_hash = documents_cache_key(dokument_quellen)
            vorlage_id, braucht_analyse = database.upsert_vorlage(
                conn,
                source.id,
                sitzung_id,
                vorlage_external_id,
                vorlagen_nr,
                dokument_hash,
                max_fehlversuche=settings.llm.max_fehlversuche_pro_vorlage,
            )

            for dokument, quelle in geladen:
                dok_id = database.upsert_dokument(conn, source.id, sitzung_id, vorlage_id, dokument)
                if isinstance(quelle, Path):
                    database.mark_dokument_heruntergeladen(conn, dok_id, file_sha256(quelle), str(quelle))

            if not braucht_analyse:
                result.bereits_analysiert += 1
                continue

            try:
                ergebnis = analyze_documents(
                    dokument_quellen, client=client, settings=settings, budget=budget, cache=cache, quelle_id=source.id
                )
            except Exception as exc:  # analyze_documents fängt intern schon ab; doppelt genäht hält besser
                result.fehler.append(f"Analyse Vorlage {vorlage_external_id}: {exc}")
                continue

            projekt_ergebnis = database.speichere_analyse_ergebnis(
                conn,
                vorlage_id=vorlage_id,
                quelle_id=source.id,
                bundesland=source.bundesland,
                kommune=source.name,
                ergebnis=ergebnis,
            )
            if ergebnis.status is AnalyseStatus.RELEVANT:
                result.treffer += 1
                if projekt_ergebnis is None:
                    # Laut radar.database kann das nicht passieren (RELEVANT impliziert
                    # extraktion gesetzt, siehe speichere_analyse_ergebnis()) - dieser Zweig ist
                    # ein Sicherheitsnetz gegen einen stillen Absturz, falls sich das mal ändert
                    # (CLAUDE.md: "Fail loud", nicht das Symptom eines TypeError weiterreichen).
                    logger.error("Vorlage %s: RELEVANT ohne Projekt-Ergebnis (unerwartet)", vorlage_external_id)
                else:
                    _, ist_neu = projekt_ergebnis
                    if ist_neu:
                        extraktion = ergebnis.extraktion
                        result.neue_treffer.append(
                            NeuerTreffer(
                                kommune=source.name,
                                titel=extraktion.projektbezeichnung or "(ohne Bezeichnung)",
                                we_gesamt=extraktion.we_gesamt,
                                url=sitzung.url or source.base_url,
                            )
                        )
            elif ergebnis.status is AnalyseStatus.FEHLER:
                result.fehler.append(f"Analyse Vorlage {vorlage_external_id}: {ergebnis.grund}")
            elif ergebnis.status is AnalyseStatus.BUDGET_ERSCHOEPFT:
                result.ohne_llm += 1

    result.vorlagen = len(vorlagen_ids)


def run_source_full(
    source: Source,
    settings: Settings,
    conn: sqlite3.Connection,
    client: anthropic.Anthropic,
    budget: LLMBudget,
    cache: ResultCache,
    start: date,
    end: date,
) -> SourceRunResult:
    """Wie `run_source()`, mit Persistenz und LLM-Analyse. Fehler werden gefangen und im Ergebnis
    protokolliert, nie weitergereicht (CLAUDE.md: "Quellen sind voneinander isoliert")."""
    result = SourceRunResult(source_id=source.id)
    database.ensure_quelle(conn, source)  # Fremdschlüssel-Voraussetzung, bevor irgendetwas gespeichert wird
    gesperrt_vorher = budget.gesperrt_grund
    try:
        http = HttpClient(source, settings)
        adapter = get_adapter(source, http)
        _collect_and_persist(adapter, http, conn, client, budget, cache, source, settings, start, end, result)
    except Exception as exc:
        result.fehler.append(str(exc))
        logger.exception("Quelle '%s' fehlgeschlagen", source.id)
    result.llm_anfragen = budget.anfragen(source.id)
    if budget.gesperrt_grund is not None and gesperrt_vorher is None:
        # Ehrlich im Quellen-Monitoring und im Exit-Code zeigen, nicht nur im Log verstecken.
        result.fehler.append(f"LLM-Notbremse ausgelöst: {budget.gesperrt_grund}")
    return result


def run_full(
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    client: anthropic.Anthropic | None = None,
) -> list[SourceRunResult]:
    """Vollständiger Lauf: Scraper -> Parser -> Datenbank für alle aktivierten Quellen (Tier A
    zuerst). Ein gemeinsames `LLMBudget` gilt über den ganzen Lauf. `conn`/`client` lassen sich für
    Tests injizieren; ohne Angabe wird die konfigurierte Datenbank/der echte Anthropic-Client
    verwendet (und am Ende geschlossen, wenn `run_full` die Verbindung selbst geöffnet hat).

    Läuft bereits ein anderer Lauf, wird sofort `LaufLaeuftBereits` geworfen (siehe `lauf_sperre`)."""
    settings = settings or load_settings()
    with lauf_sperre(settings):
        return _run_full_gesperrt(settings, conn, client)


def _run_full_gesperrt(
    settings: Settings, conn: sqlite3.Connection | None, client: anthropic.Anthropic | None
) -> list[SourceRunResult]:
    own_conn = conn is None
    conn = conn or database.connect(settings)
    client = client or make_client(settings)
    budget = LLMBudget(
        settings.llm.max_calls_per_run,
        settings.llm.max_calls_per_source,
        max_consecutive_errors=settings.llm.max_consecutive_errors,
    )
    cache = ResultCache(settings.resolve_path(settings.llm.cache_dir))

    sources = enabled_sources(load_sources(settings), settings)
    start, end = scan_range(settings)

    results = []
    try:
        for source in sources:
            logger.info("Quelle '%s' (%s): Zeitraum %s bis %s", source.id, source.name, start, end)
            result = run_source_full(source, settings, conn, client, budget, cache, start, end)
            database.record_source_run(conn, source, result)
            results.append(result)
            logger.info(
                "Quelle '%s': %d Sitzungen, %d relevant, %d Vorlagen (%d bereits analysiert), "
                "%d Dokumente, %d Downloads, %d ohne Download (Text), %d Treffer, %d LLM-Anfragen, %d Fehler",
                result.source_id,
                result.sitzungen,
                result.relevante_sitzungen,
                result.vorlagen,
                result.bereits_analysiert,
                result.dokumente,
                result.heruntergeladen,
                result.text_dokumente,
                result.treffer,
                result.llm_anfragen,
                len(result.fehler),
            )
            if result.ohne_llm:
                logger.warning(
                    "Quelle '%s': %d Vorlage(n) ohne LLM-Analyse (Budget/Notbremse), nächster Lauf versucht es erneut",
                    result.source_id,
                    result.ohne_llm,
                )
            for fehler in result.fehler:
                logger.warning("Quelle '%s': %s", result.source_id, fehler)
    finally:
        if own_conn:
            conn.close()
        log = logger.error if budget.gesperrt_grund is not None else logger.info
        log("LLM-Verbrauch dieses Laufs: %s", budget.zusammenfassung())

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_full()
