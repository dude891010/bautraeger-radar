"""Konfiguration laden: globale Einstellungen, Quellenregister und Secrets.

- config/settings.yaml  -> Settings (Filter, Scraper, LLM, Regionen)
- config/sources.yaml   -> Liste von Source (ein Eintrag pro Ratsinformationssystem)
- Secrets nur aus der Umgebung/.env, nie aus YAML.

Bewusst nur dataclasses + PyYAML. Unbekannte Schlüssel in der YAML führen zu einem Fehler
(Tippfehler sofort sichtbar).
"""

import os
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

try:  # .env nur laden, wenn python-dotenv installiert ist (lokale Entwicklung)
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"

BUNDESLAENDER = {"SH", "HH", "NI", "HB", "MV", "BE"}
SYSTEMS = {"sessionnet", "allris", "oparl", "hamburg_transparenz", "bv_hh", "unknown"}
STATUSES = {"verified", "candidate", "blocked"}
TIERS = {"A", "B", "C"}
UNKNOWN_POLICIES = {"flag", "drop"}
MAX_CALLS_PER_RUN_HARD_LIMIT = 1000  # llm.max_calls_per_run darf nie höher konfiguriert werden
MAX_CALLS_PER_DAY_HARD_LIMIT = 3000  # llm.max_calls_per_day ebenso
_ID_RE = re.compile(r"^[a-z0-9_-]+$")


# --- globale Einstellungen ------------------------------------------------------------------
@dataclass
class ScanConfig:
    months_back: int = 3
    months_ahead: int = 2


@dataclass
class ScraperConfig:
    user_agent_template: str = "BautraegerRadar/0.1 (+{contact})"
    request_delay_seconds: float = 2.0
    max_parallel_hosts: int = 1
    connect_timeout_seconds: float = 10
    read_timeout_seconds: float = 30
    max_retries: int = 3
    backoff_factor: float = 1.5
    respect_robots_txt: bool = True
    max_pdf_size_mb: int = 25
    raw_cache_dir: str = "data/raw"

    def __post_init__(self):
        if self.request_delay_seconds < 0:
            raise ValueError("scraper.request_delay_seconds darf nicht negativ sein")
        if self.max_parallel_hosts < 1:
            raise ValueError("scraper.max_parallel_hosts muss >= 1 sein")


@dataclass
class FiltersConfig:
    min_units: int = 6
    count_basis: str = "rental_or_subsidized"
    exclude_pure_ownership: bool = True
    unknown_units_policy: str = "flag"
    unknown_tenure_policy: str = "flag"
    default_committee_patterns: list[str] = field(default_factory=list)
    prefilter_keywords: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.min_units < 1:
            raise ValueError("filters.min_units muss >= 1 sein")
        for name in ("unknown_units_policy", "unknown_tenure_policy"):
            if getattr(self, name) not in UNKNOWN_POLICIES:
                raise ValueError(f"filters.{name} muss einer von {sorted(UNKNOWN_POLICIES)} sein")
        for pattern in self.default_committee_patterns:
            re.compile(pattern)  # ungültige Regex sofort melden


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    triage_model: str = ""
    extraction_model: str = ""
    temperature: float = 0.0
    max_tokens: int = 2000
    max_input_chars: int = 60000
    # Kostenbremse: gezählt wird JEDE HTTP-Anfrage an die API, inkl. Retries und Reparaturversuchen
    # (radar/parser.py: LLMBudget/_call).
    max_calls_per_run: int = 300
    max_calls_per_source: int = 40
    # Über alle Läufe eines Kalendertages (in der Datenbank gezählt): sonst bekäme jeder Klick auf
    # "Lauf jetzt starten" wieder ein volles max_calls_per_run.
    max_calls_per_day: int = 600
    max_attempts_per_call: int = 3       # Versuche je Aufruf bei vorübergehenden Fehlern (429/5xx/Timeout)
    max_consecutive_errors: int = 5      # Notbremse: so viele API-Fehler in Folge -> Rest des Laufs ohne LLM
    request_timeout_seconds: float = 120.0
    max_fehlversuche_pro_vorlage: int = 3  # danach wird eine immer wieder scheiternde Vorlage nicht mehr versucht
    cache_dir: str = "data/llm_cache"    # Ergebnisse pro Dokument-Hash, spart wiederholte Aufrufe
    prompts_dir: str = "config/prompts"
    context_pages: int = 1               # Nachbarseiten um einen Keyword-Treffer herum mitnehmen

    def __post_init__(self):
        # Harte Obergrenze gegen Tippfehler in settings.yaml (z. B. 30000 statt 300): ein Lauf soll
        # nie unbemerkt ein Vielfaches der üblichen Kosten verursachen können.
        if not 0 <= self.max_calls_per_run <= MAX_CALLS_PER_RUN_HARD_LIMIT:
            raise ValueError(f"llm.max_calls_per_run muss zwischen 0 und {MAX_CALLS_PER_RUN_HARD_LIMIT} liegen")
        if not 0 <= self.max_calls_per_day <= MAX_CALLS_PER_DAY_HARD_LIMIT:
            raise ValueError(f"llm.max_calls_per_day muss zwischen 0 und {MAX_CALLS_PER_DAY_HARD_LIMIT} liegen")
        if not 0 <= self.max_calls_per_source <= self.max_calls_per_run:
            raise ValueError("llm.max_calls_per_source muss zwischen 0 und llm.max_calls_per_run liegen")
        if not 1 <= self.max_attempts_per_call <= 5:
            raise ValueError("llm.max_attempts_per_call muss zwischen 1 und 5 liegen")
        if self.max_consecutive_errors < 1:
            raise ValueError("llm.max_consecutive_errors muss >= 1 sein")
        if not 0 < self.request_timeout_seconds <= 600:
            raise ValueError("llm.request_timeout_seconds muss zwischen 0 und 600 liegen")
        if self.max_fehlversuche_pro_vorlage < 1:
            raise ValueError("llm.max_fehlversuche_pro_vorlage muss >= 1 sein")


@dataclass
class DatabaseConfig:
    path: str = "data/radar.db"


@dataclass
class DashboardConfig:
    statuses: list[str] = field(default_factory=lambda: ["Neu"])


@dataclass
class SchedulerConfig:
    """Geplanter Lauf (Schritt 7, radar/scheduler.py). `weekday`: 0=Montag ... 6=Sonntag."""

    weekday: int = 0
    hour: int = 3
    minute: int = 0
    check_interval_seconds: int = 60

    def __post_init__(self):
        if not 0 <= self.weekday <= 6:
            raise ValueError("scheduler.weekday muss zwischen 0 (Montag) und 6 (Sonntag) liegen")
        if not 0 <= self.hour <= 23:
            raise ValueError("scheduler.hour muss zwischen 0 und 23 liegen")
        if not 0 <= self.minute <= 59:
            raise ValueError("scheduler.minute muss zwischen 0 und 59 liegen")
        if self.check_interval_seconds < 1:
            raise ValueError("scheduler.check_interval_seconds muss >= 1 sein")


@dataclass
class RegionConfig:
    name: str
    tier: str = "B"
    min_units: int | None = None

    def __post_init__(self):
        if self.tier not in TIERS:
            raise ValueError(f"Region {self.name}: tier muss einer von {sorted(TIERS)} sein")
        if self.min_units is not None and self.min_units < 1:
            raise ValueError(f"Region {self.name}: min_units muss >= 1 sein")


# --- Quellen ---------------------------------------------------------------------------------
@dataclass
class Source:
    id: str
    name: str
    bundesland: str
    system: str
    base_url: str
    status: str = "candidate"
    enabled: bool = False
    tier: str | None = None
    min_units: int | None = None
    committees: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    notes: str = ""
    # Weitere Hosts, von denen dieselbe Quelle Dateien laden darf (z. B. ein offizielles
    # Open-Data-Ökosystem, das API und Dateiablage auf verschiedenen Domains betreibt - siehe
    # hamburg_transparenz: API unter suche.transparenz.hamburg.de, PDFs unter daten-hamburg.de).
    # Jeder zusätzliche Host bekommt seine eigene, unabhängig geprüfte robots.txt (siehe
    # HttpClient); `ignore_robots_txt` gilt nur für `base_url`s Host, nie für diese hier.
    additional_hosts: list[str] = field(default_factory=list)
    request_delay_seconds: float | None = None  # Quelle > global (scraper.request_delay_seconds)
    # Nur mit ausführlicher Begründung in `notes` setzen (siehe __post_init__): eine robots.txt zu
    # ignorieren ist die Ausnahme, nicht die Regel (CLAUDE.md, Abschnitt "Scraping-Regeln"). Bisher
    # einzige Verwendung: transparenz.hamburg.de sperrt /api/ pauschal, dokumentiert und lizenziert
    # denselben Pfad aber selbst ausdrücklich zur automatisierten Nutzung (siehe notes der Quelle).
    ignore_robots_txt: bool = False

    def __post_init__(self):
        if not _ID_RE.match(self.id):
            raise ValueError(f"Quelle '{self.id}': id nur aus [a-z0-9_-]")
        if self.bundesland not in BUNDESLAENDER:
            raise ValueError(f"Quelle '{self.id}': bundesland muss einer von {sorted(BUNDESLAENDER)} sein")
        if self.system not in SYSTEMS:
            raise ValueError(f"Quelle '{self.id}': system muss einer von {sorted(SYSTEMS)} sein")
        if self.status not in STATUSES:
            raise ValueError(f"Quelle '{self.id}': status muss einer von {sorted(STATUSES)} sein")
        if self.tier is not None and self.tier not in TIERS:
            raise ValueError(f"Quelle '{self.id}': tier muss einer von {sorted(TIERS)} sein")
        if urlparse(self.base_url).scheme != "https":
            raise ValueError(f"Quelle '{self.id}': base_url muss mit https:// beginnen")
        if self.enabled and (self.status != "verified" or self.system == "unknown"):
            raise ValueError(
                f"Quelle '{self.id}': aktiviert werden dürfen nur verifizierte Quellen "
                "mit bekanntem System (status: verified)"
            )
        if self.request_delay_seconds is not None and self.request_delay_seconds < 0:
            raise ValueError(f"Quelle '{self.id}': request_delay_seconds darf nicht negativ sein")
        for host in self.additional_hosts:
            if "://" in host or "/" in host:
                raise ValueError(f"Quelle '{self.id}': additional_hosts muss reine Hostnamen enthalten, nicht '{host}'")
        if self.ignore_robots_txt and not self.notes.strip():
            raise ValueError(
                f"Quelle '{self.id}': ignore_robots_txt=true braucht eine Begründung in 'notes' "
                "(robots.txt zu ignorieren ist die Ausnahme, nicht die Regel)"
            )
        for pattern in self.committees:
            re.compile(pattern)

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""


@dataclass
class Settings:
    scan: ScanConfig
    scraper: ScraperConfig
    filters: FiltersConfig
    llm: LLMConfig
    database: DatabaseConfig
    dashboard: DashboardConfig
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    sources_file: str = "config/sources.yaml"
    regions: dict = field(default_factory=dict)  # Bundesland-Kürzel -> RegionConfig

    # --- Ableitungen je Quelle -------------------------------------------------------------
    def min_units_for(self, source: Source) -> int:
        """Quelle > Region > global."""
        if source.min_units is not None:
            return source.min_units
        region = self.regions.get(source.bundesland)
        if region is not None and region.min_units is not None:
            return region.min_units
        return self.filters.min_units

    def tier_for(self, source: Source) -> str:
        """Quelle > Region > 'B'."""
        if source.tier is not None:
            return source.tier
        region = self.regions.get(source.bundesland)
        return region.tier if region is not None else "B"

    def committee_patterns_for(self, source: Source) -> list[str]:
        return source.committees or self.filters.default_committee_patterns

    def request_delay_seconds_for(self, source: Source) -> float:
        """Quelle > global. Für Quellen mit strengerem `Crawl-Delay` in der robots.txt (z. B.
        transparenz.hamburg.de: 10s) in sources.yaml überschreibbar."""
        if source.request_delay_seconds is not None:
            return source.request_delay_seconds
        return self.scraper.request_delay_seconds

    # --- Secrets / Umgebung (nie aus der YAML) ---------------------------------------------
    @property
    def anthropic_api_key(self) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt (siehe .env.example)")
        return key

    @property
    def user_agent(self) -> str:
        contact = os.environ.get("SCRAPER_CONTACT", "").strip()
        if not contact:
            raise RuntimeError(
                "SCRAPER_CONTACT ist nicht gesetzt: ein identifizierbarer User-Agent "
                "mit Kontaktadresse ist Pflicht (siehe .env.example)"
            )
        return self.scraper.user_agent_template.format(contact=contact)

    def resolve_path(self, relative: str) -> Path:
        """Pfad relativ zum Projektverzeichnis auflösen (Docker-/CWD-unabhängig)."""
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


def _build(cls, data):
    data = data or {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Unbekannte Konfigurationsschlüssel in {cls.__name__}: {sorted(unknown)}")
    kwargs = {}
    for name, f in known.items():
        if name in data:
            value = data[name]
            kwargs[name] = _build(f.type, value) if is_dataclass(f.type) else value
        elif is_dataclass(f.type):
            kwargs[name] = _build(f.type, {})
    return cls(**kwargs)


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(path: str | Path | None = None) -> Settings:
    """Einstellungen laden. Reihenfolge: Argument > $RADAR_CONFIG > config/settings.yaml."""
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")
    config_path = Path(path or os.environ.get("RADAR_CONFIG") or DEFAULT_CONFIG)
    raw = _read_yaml(config_path)
    raw_regions = raw.pop("regions", {}) or {}
    settings = _build(Settings, raw)
    for code, region in raw_regions.items():
        if code not in BUNDESLAENDER:
            raise ValueError(f"regions: unbekanntes Bundesland-Kürzel '{code}'")
        settings.regions[code] = _build(RegionConfig, region)
    return settings


def load_sources(settings: Settings, path: str | Path | None = None) -> list[Source]:
    """Quellenregister laden und validieren (eindeutige IDs, Region vorhanden)."""
    sources_path = Path(path) if path else settings.resolve_path(settings.sources_file)
    raw = _read_yaml(sources_path)
    unknown = set(raw) - {"sources"}
    if unknown:
        raise ValueError(f"Unbekannte Schlüssel in {sources_path.name}: {sorted(unknown)}")
    sources = [_build(Source, entry) for entry in raw.get("sources", [])]
    seen: set[str] = set()
    for src in sources:
        if src.id in seen:
            raise ValueError(f"Doppelte Quellen-ID: {src.id}")
        seen.add(src.id)
        if src.bundesland not in settings.regions:
            raise ValueError(f"Quelle '{src.id}': Bundesland {src.bundesland} fehlt unter regions in settings.yaml")
    return sources


def enabled_sources(sources: list[Source], settings: Settings) -> list[Source]:
    """Aktive Quellen, sortiert nach Tier (A zuerst), dann Name."""
    return sorted((s for s in sources if s.enabled), key=lambda s: (settings.tier_for(s), s.name))
