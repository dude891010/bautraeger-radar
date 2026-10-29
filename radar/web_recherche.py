"""Web-Recherche nach konkreten Bauvorhaben im Umkreis um Norderstedt (Phase 3 + 4).

Ergänzt die Ratsunterlagen (radar/scraper.py) um die beiden Phasen, die dort oft fehlen:
  Phase 3 - Baugenehmigung erteilt
  Phase 4 - Baubeginn (Spatenstich, Grundsteinlegung, Richtfest)
Gerade diese Phasen liegen zeitlich am nächsten am Küchenbedarf (Kundenfeedback Hummel, 10/2026),
stehen aber eher in Lokalpresse und Pressemitteilungen von Bauträgern als in Ausschussvorlagen.

Ablauf (`fuehre_recherche_aus()`):
  1. Orte aus config/web_recherche.yaml, die per Luftlinie im Umkreis (`radius_km`) um das Zentrum
     liegen - Koordinaten stehen in der Konfiguration, kein Geocoding-Dienst nötig.
  2. Je Ort und Phase eine Suchanfrage an die Such-API (Tavily).
  3. Jedes Ergebnis wird **ohne LLM** per Stichworten geprüft: Ort genannt? Wohnbau? Phase 3/4?
     Unpassendes wird verworfen. Eine WE-Zahl wird nur übernommen, wenn sie wörtlich im Text steht
     (mit Zitat als Beleg, CLAUDE.md: "Zahlen nur aus dem Text").
  4. Speichern in `web_funde` (radar/database.py), Dedup über die URL. Status/Notizen pflegt nur
     das Dashboard, ein späterer Lauf überschreibt sie nie.

Warum Tavily und nicht Perplexity: Tavily liefert echte Suchergebnisse (Titel, URL, Textauszug),
Perplexity eine KI-formulierte Antwort. Für den Vertrieb muss jeder Fund auf eine nachprüfbare
Quelle zeigen; eine generierte Zusammenfassung könnte WE-Zahlen oder Bauherren erfinden. Die
Such-API steckt hinter dem schmalen `SuchAnbieter`-Protokoll, ein anderer Anbieter lässt sich
später ergänzen, ohne Lauf, Datenbank oder Dashboard anzufassen.

Kostenschutz wie beim LLM: jede Anfrage zählt gegen `max_anfragen_pro_lauf` und, über alle Läufe,
gegen `max_anfragen_pro_tag` (Tabelle `such_nutzung`). Konto-Fehler (Key ungültig, Kontingent
leer) und `max_fehler_in_folge` Fehler in Folge stoppen den Lauf sofort.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse, urlunparse

import requests

from radar import database
from radar.config import (
    BUNDESLAENDER,
    LIVE_AUS_HINWEIS,
    DatabaseConfig,
    Settings,
    _build,
    _read_yaml,
    load_settings,
)
from radar.notifier import NeuerWebFund

logger = logging.getLogger(__name__)

DEFAULT_WEB_CONFIG = "config/web_recherche.yaml"
MAX_ANFRAGEN_HARD_LIMIT = 500  # max_anfragen_pro_lauf/-tag dürfen nie höher konfiguriert werden
ZEITRAEUME = {"day", "week", "month", "year"}
SUCHTIEFEN = {"basic", "advanced"}
MAX_AUSZUG_ZEICHEN = 600
MAX_EVIDENZ_ZEICHEN = 200


class Phase(StrEnum):
    BAUGENEHMIGUNG = "BAUGENEHMIGUNG"  # Phase 3
    BAUBEGINN = "BAUBEGINN"  # Phase 4


PHASE_LABEL = {
    Phase.BAUGENEHMIGUNG: "Phase 3 – Baugenehmigung",
    Phase.BAUBEGINN: "Phase 4 – Baubeginn",
}
# Höhere Zahl = weiter fortgeschritten. Taucht derselbe Artikel später mit Baubeginn auf, wird die
# Phase hochgestuft, nie zurück.
PHASE_RANG = {Phase.BAUGENEHMIGUNG: 3, Phase.BAUBEGINN: 4}


# -- Konfiguration --------------------------------------------------------------------------------


@dataclass
class Ort:
    name: str
    bundesland: str
    lat: float
    lon: float
    suchname: str | None = None  # Begriff für die Suchanfrage, falls abweichend vom Namen
    stichwort: str | None = None  # Wort, das im Treffer stehen muss (Standard: name)

    def __post_init__(self):
        if self.bundesland not in BUNDESLAENDER:
            raise ValueError(f"Ort '{self.name}': bundesland muss einer von {sorted(BUNDESLAENDER)} sein")
        if not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise ValueError(f"Ort '{self.name}': ungültige Koordinaten")

    @property
    def such_begriff(self) -> str:
        return self.suchname or self.name

    @property
    def erkennungs_wort(self) -> str:
        return self.stichwort or self.name


@dataclass
class WebRechercheConfig:
    anbieter: str = "tavily"
    zentrum_name: str = "Norderstedt"
    zentrum_lat: float = 53.7064
    zentrum_lon: float = 9.9922
    radius_km: float = 50
    suchanfragen: dict[str, list[str]] = field(default_factory=dict)
    phasen_stichworte: dict[str, list[str]] = field(default_factory=dict)
    wohn_stichworte: list[str] = field(default_factory=list)
    # Steht einer dieser Begriffe direkt um ein Phase-4-Stichwort ("Baubeginn ist für 2027 geplant"),
    # ist der Baubeginn nur angekündigt - diese Stelle zählt dann nicht als Phase 4.
    zukunft_stichworte: list[str] = field(default_factory=list)
    # Wohnform-Erkennung (config/web_recherche.yaml); Mindestgröße für reine Eigentumsprojekte setzt
    # load_web_config() aus settings.yaml (filters.eigentum_min_we).
    eigentum_stichworte: list[str] = field(default_factory=list)
    miet_stichworte: list[str] = field(default_factory=list)
    eigentum_min_we: int = 12
    ausgeschlossene_domains: list[str] = field(default_factory=list)
    max_ergebnisse_pro_suche: int = 8
    zeitraum: str = "year"
    suchtiefe: str = "basic"
    max_anfragen_pro_lauf: int = 120
    max_anfragen_pro_tag: int = 240
    max_fehler_in_folge: int = 3
    request_delay_seconds: float = 1.0
    timeout_seconds: float = 30
    orte: list[Ort] = field(default_factory=list)

    def __post_init__(self):
        if self.anbieter != "tavily":
            raise ValueError("web_recherche: anbieter muss 'tavily' sein (bisher einziger Adapter)")
        if self.radius_km <= 0:
            raise ValueError("web_recherche: radius_km muss > 0 sein")
        for name in ("max_anfragen_pro_lauf", "max_anfragen_pro_tag"):
            if not 0 <= getattr(self, name) <= MAX_ANFRAGEN_HARD_LIMIT:
                raise ValueError(f"web_recherche: {name} muss zwischen 0 und {MAX_ANFRAGEN_HARD_LIMIT} liegen")
        if not 1 <= self.max_ergebnisse_pro_suche <= 20:
            raise ValueError("web_recherche: max_ergebnisse_pro_suche muss zwischen 1 und 20 liegen")
        if self.zeitraum not in ZEITRAEUME:
            raise ValueError(f"web_recherche: zeitraum muss einer von {sorted(ZEITRAEUME)} sein")
        if self.suchtiefe not in SUCHTIEFEN:
            raise ValueError(f"web_recherche: suchtiefe muss einer von {sorted(SUCHTIEFEN)} sein")
        if self.eigentum_min_we < 1:
            raise ValueError("web_recherche: eigentum_min_we muss >= 1 sein")
        if self.max_fehler_in_folge < 1:
            raise ValueError("web_recherche: max_fehler_in_folge muss >= 1 sein")
        if self.request_delay_seconds < 0 or not 0 < self.timeout_seconds <= 120:
            raise ValueError("web_recherche: request_delay_seconds >= 0, timeout_seconds zwischen 0 und 120")
        gueltige_phasen = {p.value for p in Phase}
        for name in ("suchanfragen", "phasen_stichworte"):
            unbekannt = set(getattr(self, name)) - gueltige_phasen
            if unbekannt:
                raise ValueError(f"web_recherche.{name}: unbekannte Phase(n) {sorted(unbekannt)}")
        for vorlagen in self.suchanfragen.values():
            for vorlage in vorlagen:
                if "{ort}" not in vorlage:
                    raise ValueError(f"web_recherche.suchanfragen: '{vorlage}' enthält kein {{ort}}")


def load_web_config(settings: Settings, path: str | Path | None = None) -> WebRechercheConfig:
    """config/web_recherche.yaml laden. Unbekannte Schlüssel sind wie in radar/config.py ein Fehler.
    `eigentum_min_we` kommt aus settings.yaml (filters.eigentum_min_we), gemeinsam mit den Ratsunterlagen."""
    pfad = Path(path) if path else settings.resolve_path(DEFAULT_WEB_CONFIG)
    raw = _read_yaml(pfad)
    if "eigentum_min_we" in raw:
        raise ValueError(
            "web_recherche.yaml: eigentum_min_we steht jetzt in config/settings.yaml unter filters "
            "(eine Grenze für Ratsunterlagen und Web-Recherche)"
        )
    roh_orte = raw.pop("orte", []) or []
    config = _build(WebRechercheConfig, {**raw, "eigentum_min_we": settings.filters.eigentum_min_we})
    config.orte = [_build(Ort, eintrag) for eintrag in roh_orte]
    return config


def tavily_api_key() -> str:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TAVILY_API_KEY ist nicht gesetzt (siehe .env.example)")
    return key


# -- Geografie ----------------------------------------------------------------------------------


def entfernung_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Luftlinie (Haversine). Für einen 50-km-Umkreis genau genug; Straßenentfernung ist bewusst
    kein Ziel (bräuchte einen Routing-Dienst)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def orte_im_radius(config: WebRechercheConfig) -> list[tuple[Ort, float]]:
    """Orte innerhalb von `radius_km`, nächster zuerst (wird das Budget knapp, kommen so die
    nahen Orte zuerst dran)."""
    mit_entfernung = [
        (ort, round(entfernung_km(config.zentrum_lat, config.zentrum_lon, ort.lat, ort.lon), 1))
        for ort in config.orte
    ]
    return sorted(((o, e) for o, e in mit_entfernung if e <= config.radius_km), key=lambda t: t[1])


@dataclass
class Suchauftrag:
    ort: Ort
    entfernung_km: float
    phase: Phase
    anfrage: str


def baue_suchauftraege(config: WebRechercheConfig) -> list[Suchauftrag]:
    auftraege = []
    for ort, entfernung in orte_im_radius(config):
        for phase in Phase:
            for vorlage in config.suchanfragen.get(phase.value, []):
                anfrage = vorlage.format(ort=ort.such_begriff)
                auftraege.append(Suchauftrag(ort=ort, entfernung_km=entfernung, phase=phase, anfrage=anfrage))
    return auftraege


# -- Such-API -------------------------------------------------------------------------------------


@dataclass
class Suchergebnis:
    titel: str
    url: str
    inhalt: str
    veroeffentlicht: str | None = None


class SuchAnbieter(Protocol):
    def suche(self, anfrage: str) -> list[Suchergebnis]: ...


class LiveAufrufeDeaktiviert(RuntimeError):
    """Echte Suche verlangt, aber betrieb.live_api_aufrufe ist aus (siehe radar/config.BetriebConfig)."""


def live_aufrufe_erlaubt(settings: Settings | None = None) -> bool:
    return (settings or load_settings()).betrieb.live_api_aufrufe


class SucheGesperrt(Exception):
    """Jede weitere Anfrage würde genauso scheitern (Key ungültig, Kontingent aufgebraucht)."""


# -- Demo-Anbieter: Live-Präsentation ohne API-Key und ohne Credits ----------------------------------
# Frei erfundene Suchergebnisse (fiktive Bauträger, URLs auf example.org, Titel mit "(Demo)"). Sie
# laufen durch dieselbe Bewertung wie echte Treffer (`bewerte_ergebnis()`) - inklusive Ergebnissen,
# die bewusst verworfen werden (Gewerbe ohne Wohnen, Immobilienportal, B-Plan ohne Phase 3/4), damit
# die Demo auch die Filterlogik zeigt. Bewusst KEINE echten Firmen- oder Projektnamen: eine Demo
# soll nichts als Fund präsentieren, was so nicht belegt ist.
_DEMO = "https://example.org/demo/"

# Einzige echte Ausnahme in den Demo-Daten: ein reales, an zwei Quellen geprüftes Projekt als Highlight
# (geprüft am 27.10.2026). Titel = wörtlicher Titel der konii.de-Meldung vom 05.06.2026; der Auszug ist
# aus den übereinstimmenden Angaben beider Quellen zusammengestellt (nicht wörtlich zitiert):
#   https://www.konii.de/news/grundstein-gelegt-instone-startet-bau-des-wohnquartiers-nordlicht-in-norderstedt/
#   https://www.entwicklungsstadt.de/norderstedt-500-wohnungen-noerdlich-von-hamburg-geplant/ (09.06.2026)
# Fakten: Instone Real Estate, Kösliner Weg 7-9, 198 Eigentumswohnungen in neun Gebäuden, rund 60
# barrierefrei, Grundstein gelegt (Phase 4), Fertigstellung 2029, Förderanteil nicht genannt.
# Ohne abschließenden "/": so wie `normalisiere_url()` sie speichert (konii.de leitet auf die Fassung mit
# "/" weiter), sonst fände das Dashboard den gespeicherten Fund in VERIFIZIERTE_DEMO_URLS nicht wieder.
NORDLICHT_URL = (
    "https://www.konii.de/news/grundstein-gelegt-instone-startet-bau-des-wohnquartiers-nordlicht-in-norderstedt"
)
NORDLICHT_ZWEITQUELLE = "https://www.entwicklungsstadt.de/norderstedt-500-wohnungen-noerdlich-von-hamburg-geplant/"

# Zwei weitere echte Norderstedter Projekte (geprüft am 29.10.2026, beide Links erreichbar) - bewusst in
# einer FRÜHPHASE: sie laufen durch die normale Bewertung und werden dort verworfen (keine Baugenehmigung,
# kein Baubeginn). Die Demo zeigt damit, dass das Radar Planungen nicht als kurzfristige Leads ausgibt.
#   SonnenHeyde (Grundstücksgesellschaft Manke): Projektseite führt es unter "Geplante Projekte", ca. 144 WE
#   in Reihen-, Doppel- und Mehrfamilienhäusern am Mühlenweg (Harksheide), angeboten zum Kauf, kein Datum.
#   Richtweg (Instone): konii.de 08.06.2026, rund 316 Wohnungen, ca. 30 % gefördert, Bebauungsplan in
#   Aufstellung, Baubeginn für 2027 geplant. (Auszüge zusammengefasst, nicht wörtlich.)
SONNENHEYDE_URL = "https://www.manke-bau.de/projekte/sonnenheyde-in-norderstedt"
RICHTWEG_URL = (
    "https://www.konii.de/news/instone-erwirbt-grundstueck-im-hamburger-grossraum-weiteres-wohnprojekt-in-norderstedt-geplant"
)
# Kurzname je echtem Projekt für die Terminal-Ausgabe (die Quell-Überschriften nennen ihn nicht immer).
VERIFIZIERTE_DEMO_PROJEKTE = {
    NORDLICHT_URL: "Nordlicht (Instone)",
    SONNENHEYDE_URL: "SonnenHeyde (Manke)",
    RICHTWEG_URL: "Richtweg (Instone)",
}
VERIFIZIERTE_DEMO_URLS = set(VERIFIZIERTE_DEMO_PROJEKTE)
DEMO_SUCHERGEBNISSE: dict[tuple[str, Phase], list[Suchergebnis]] = {
    ("Norderstedt", Phase.BAUGENEHMIGUNG): [
        Suchergebnis(
            titel="SonnenHeyde, Reihen-, Doppel- und Mehrfamilienhäuser in Norderstedt",
            url=SONNENHEYDE_URL,
            inhalt="Geplantes Projekt der Grundstücksgesellschaft Manke am Mühlenweg in Norderstedt-Harksheide: "
            "ca. 144 Wohneinheiten, aufgeteilt auf Reihen- und Doppelhäuser und Geschossbau, zum Kauf. "
            "Termine sind noch nicht veröffentlicht.",
        ),
        Suchergebnis(
            titel="Instone erwirbt Grundstück im Hamburger Großraum – weiteres Wohnprojekt in Norderstedt geplant",
            url=RICHTWEG_URL,
            inhalt="Instone hat am Richtweg im Garstedter Dreieck in Norderstedt ein Grundstück für rund 316 "
            "Wohnungen erworben, davon ca. 30 % öffentlich gefördert. Das Bebauungsplanverfahren ist in "
            "Aufstellung, der Baubeginn ist für 2027 geplant.",
            veroeffentlicht="2026-06-08",
        ),
        Suchergebnis(
            titel="Norderstedt: Baugenehmigung für 96 Mietwohnungen am Beispielweg (Demo)",
            url=_DEMO + "norderstedt-beispielweg",
            inhalt="Die Stadt Norderstedt hat die Baugenehmigung erteilt: Die fiktive Musterbau Wohnen GmbH "
            "errichtet 96 Mietwohnungen in vier Mehrfamilienhäusern, davon 30 öffentlich gefördert. "
            "Baubeginn ist für das Frühjahr geplant.",
            veroeffentlicht="2026-10-14",
        ),
        Suchergebnis(
            titel="Norderstedt: Baugenehmigung für neue Logistikhalle (Demo)",
            url=_DEMO + "norderstedt-logistik",
            inhalt="Im Gewerbegebiet Nord hat Norderstedt die Baugenehmigung für eine Logistikhalle erteilt.",
        ),
    ],
    ("Norderstedt", Phase.BAUBEGINN): [
        Suchergebnis(
            titel="Grundstein gelegt: Instone startet Bau des Wohnquartiers „Nordlicht“ in Norderstedt",
            url=NORDLICHT_URL,
            inhalt="Instone Real Estate hat in Norderstedt den Grundstein für das Wohnquartier „Nordlicht“ gelegt. "
            "Am Kösliner Weg 7–9 entstehen 198 Eigentumswohnungen in neun Gebäuden, rund 60 davon "
            f"barrierefrei. Fertigstellung 2029. Weitere Quelle: {NORDLICHT_ZWEITQUELLE}",
            veroeffentlicht="2026-06-05",
        ),
        Suchergebnis(
            titel="Spatenstich im Quartier Am Musterpark in Norderstedt (Demo)",
            url=_DEMO + "norderstedt-musterpark",
            inhalt="Mit dem Spatenstich haben in Norderstedt die Arbeiten am Quartier Am Musterpark begonnen. "
            "Es entstehen 140 Wohnungen, ein Drittel davon gefördert, Fertigstellung 2028.",
            veroeffentlicht="2026-09-30",
        ),
        Suchergebnis(
            titel="Norderstedt: Neubau-Eigentumswohnung, Baubeginn erfolgt (Demo)",
            url="https://www.immobilienscout24.de/expose/demo-0000",
            inhalt="3-Zimmer-Wohnung in Norderstedt kaufen, Baubeginn erfolgt.",
        ),
    ],
    ("Quickborn", Phase.BAUBEGINN): [
        Suchergebnis(
            titel="Quickborn: Baubeginn für 8 Eigentumswohnungen (Demo)",
            url=_DEMO + "quickborn-etw-klein",
            inhalt="In Quickborn haben die Bauarbeiten begonnen: 8 Eigentumswohnungen in einem Stadthaus.",
        ),
    ],
    ("Quickborn", Phase.BAUGENEHMIGUNG): [
        Suchergebnis(
            titel="Grünes Licht für Wohnquartier am Bahnhof Quickborn (Demo)",
            url=_DEMO + "quickborn-bahnhof",
            inhalt="Für das Wohnquartier am Bahnhof Quickborn liegt die Baugenehmigung vor. Geplant sind "
            "38 Wohnungen in drei Mehrfamilienhäusern.",
            veroeffentlicht="2026-10-02",
        ),
    ],
    ("Ellerau", Phase.BAUBEGINN): [
        Suchergebnis(
            titel="Richtfest im Ellerauer Wohnprojekt (Demo)",
            url=_DEMO + "ellerau-richtfest",
            inhalt="Richtfest in Ellerau: Das Wohnprojekt mit Mietwohnungen für Senioren und Familien liegt "
            "im Zeitplan, Bezug ist für Herbst 2027 vorgesehen.",
            veroeffentlicht="2026-08-27",
        ),
        Suchergebnis(
            titel="Ellerau plant neues Wohngebiet (Demo)",
            url=_DEMO + "ellerau-bplan",
            inhalt="Ellerau stellt einen Bebauungsplan für rund 60 Wohnungen auf, Bürgerbeteiligung im Winter.",
        ),
    ],
    ("Henstedt-Ulzburg", Phase.BAUBEGINN): [
        Suchergebnis(
            titel="Baustart für 64 Mietwohnungen in Henstedt-Ulzburg (Demo)",
            url=_DEMO + "henstedt-ulzburg-baustart",
            inhalt="In Henstedt-Ulzburg ist Baustart: 64 Mietwohnungen, davon 20 öffentlich gefördert.",
            veroeffentlicht="2026-09-18",
        ),
    ],
    ("Hamburg-Eimsbüttel", Phase.BAUGENEHMIGUNG): [
        Suchergebnis(
            titel="Eimsbüttel: Baugenehmigung für Drittelmix-Projekt (Demo)",
            url=_DEMO + "eimsbuettel-drittelmix",
            inhalt="Das Bezirksamt Eimsbüttel hat die Baugenehmigung für 210 Wohnungen im Drittelmix erteilt.",
            veroeffentlicht="2026-10-20",
        ),
    ],
    ("Buxtehude", Phase.BAUGENEHMIGUNG): [
        Suchergebnis(
            titel="Grünes Licht für 120 Wohnungen in Buxtehude (Demo)",
            url=_DEMO + "buxtehude-este",
            inhalt="Der Landkreis hat die Baugenehmigung für 120 Wohnungen im neuen Quartier an der Este in "
            "Buxtehude erteilt, ein Drittel davon gefördert.",
            veroeffentlicht="2026-07-15",
        ),
    ],
}


class DemoAnbieter:
    """Ersetzt die Such-API für Live-Demos: liefert `DEMO_SUCHERGEBNISSE` passend zu Ort und Phase
    der Anfrage. `kostenpflichtig = False` - `fuehre_recherche_aus()` zählt diese Anfragen nicht
    gegen das Tageslimit und wartet nicht zwischen ihnen."""

    kostenpflichtig = False

    def __init__(self, config: WebRechercheConfig):
        self._auftraege = {a.anfrage: (a.ort.name, a.phase) for a in baue_suchauftraege(config)}

    def suche(self, anfrage: str) -> list[Suchergebnis]:
        return list(DEMO_SUCHERGEBNISSE.get(self._auftraege.get(anfrage), []))


class TavilyClient:
    """Dünne Hülle um die Tavily-Search-API (POST /search). Bewusst ohne das `tavily-python`-Paket:
    ein einzelner JSON-POST über das ohnehin vorhandene `requests` braucht keine neue Abhängigkeit.
    Keine automatischen Retries (jede Anfrage kostet ein Credit), keine Weiterleitungen."""

    URL = "https://api.tavily.com/search"
    # 432/433: Tavily-eigene Codes für "Plan-/Kontingent-Limit erreicht".
    KONTO_STATUS = {401, 403, 432, 433}

    def __init__(self, api_key: str, config: WebRechercheConfig, session: requests.Session | None = None):
        self._api_key = api_key
        self._config = config
        self._session = session or requests.Session()

    def suche(self, anfrage: str) -> list[Suchergebnis]:
        body = {
            "query": anfrage,
            "topic": "general",
            "search_depth": self._config.suchtiefe,
            "max_results": self._config.max_ergebnisse_pro_suche,
            "time_range": self._config.zeitraum,
            "country": "germany",
            "include_answer": False,
            "include_raw_content": False,
        }
        if self._config.ausgeschlossene_domains:
            body["exclude_domains"] = self._config.ausgeschlossene_domains
        response = self._session.post(
            self.URL,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=(10, self._config.timeout_seconds),
            allow_redirects=False,
        )
        if response.status_code in self.KONTO_STATUS:
            raise SucheGesperrt(f"Tavily HTTP {response.status_code}: {response.text[:200]}")
        response.raise_for_status()
        ergebnisse = []
        for eintrag in response.json().get("results", []):
            url = str(eintrag.get("url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            ergebnisse.append(
                Suchergebnis(
                    titel=str(eintrag.get("title") or "").strip(),
                    url=url,
                    inhalt=str(eintrag.get("content") or "").strip(),
                    veroeffentlicht=eintrag.get("published_date") or None,
                )
            )
        return ergebnisse


# -- Bewertung eines Treffers (ohne LLM) ------------------------------------------------------------


@dataclass
class WebFund:
    url: str
    titel: str
    auszug: str
    domain: str
    ort: str
    bundesland: str
    entfernung_km: float
    phase: Phase
    we_anzahl: int | None
    evidenz: str | None
    suchanfrage: str
    veroeffentlicht: str | None = None
    wohnform: str = "UNKLAR"  # EIGENTUM | MIETE | GEMISCHT | UNKLAR (siehe bestimme_wohnform)


def _enthaelt(text: str, begriffe: list[str]) -> bool:
    lowered = text.lower()
    return any(b.lower() in lowered for b in begriffe)


# Fenster um ein Phase-4-Stichwort, in dem nach Zukunftsbegriffen gesucht wird (Zeichen davor/danach).
# Klein genug, dass ein angekündigter Bezug am Satzende ("..., Bezug ist für 2027 vorgesehen") ein
# stattgefundenes Richtfest am Satzanfang nicht entwertet.
_ZUKUNFT_VORHER = 25
_ZUKUNFT_NACHHER = 45
_SATZTEIL_GRENZE = re.compile(r"[.,;:!?\n]")
_JAHR_RE = re.compile(r"\b(20\d\d)\b")


def _baubeginn_erfolgt(text: str, config: WebRechercheConfig, heute: date | None = None) -> bool:
    """Mindestens eine Erwähnung eines Phase-4-Stichworts, die nicht nur angekündigt ist. Angekündigt
    heißt: ein Zukunftsbegriff (`zukunft_stichworte`) ODER ein künftiges Jahr im selben Satzteil -
    "Den Baubeginn setzt der Entwickler für 2027 an" kommt ganz ohne "geplant"/"soll" aus."""
    lowered = text.lower()
    zukunft = [z.lower() for z in config.zukunft_stichworte]
    jahr = (heute or date.today()).year
    for stichwort in config.phasen_stichworte.get(Phase.BAUBEGINN.value, []):
        for treffer in re.finditer(re.escape(stichwort.lower()), lowered):
            # Nur derselbe Satzteil: "Baubeginn war im Mai, der Bezug ist geplant" bleibt Phase 4.
            davor = _SATZTEIL_GRENZE.split(lowered[max(0, treffer.start() - _ZUKUNFT_VORHER) : treffer.start()])[-1]
            danach = _SATZTEIL_GRENZE.split(lowered[treffer.end() : treffer.end() + _ZUKUNFT_NACHHER])[0]
            satzteil = davor + treffer.group(0) + danach
            kuenftiges_jahr = any(int(j) > jahr for j in _JAHR_RE.findall(satzteil))
            if not kuenftiges_jahr and not any(z in satzteil for z in zukunft):
                return True
    return False


def bestimme_wohnform(text: str, config: WebRechercheConfig) -> str:
    """Grobe Wohnform aus Stichworten (ohne LLM). "ETW" nur als ganzes Wort, sonst trifft es Fremdwörter."""
    eigentum = any(
        re.search(rf"\b{re.escape(s)}", text, re.IGNORECASE) if s.isupper() else s.lower() in text.lower()
        for s in config.eigentum_stichworte
    )
    miete = _enthaelt(text, config.miet_stichworte)
    if eigentum and miete:
        return "GEMISCHT"
    if eigentum:
        return "EIGENTUM"
    if miete:
        return "MIETE"
    return "UNKLAR"


def klassifiziere_phase(text: str, config: WebRechercheConfig, heute: date | None = None) -> Phase | None:
    """Phase 4 vor Phase 3: nennt ein Artikel Genehmigung UND (erfolgten) Baubeginn, ist das
    Vorhaben schon weiter. Ein nur angekündigter Baubeginn ("soll im Frühjahr starten") zählt nicht
    als Phase 4 (`zukunft_stichworte`). `None`, wenn keine der beiden Phasen erkennbar ist."""
    if _baubeginn_erfolgt(text, config, heute):
        return Phase.BAUBEGINN
    if _enthaelt(text, config.phasen_stichworte.get(Phase.BAUGENEHMIGUNG.value, [])):
        return Phase.BAUGENEHMIGUNG
    return None


# "120 Wohnungen", "48 neue Mietwohnungen", "36 öffentlich geförderte Wohneinheiten", "1.200 Wohnungen"
_WE_RE = re.compile(
    r"(?<![\d.,])(\d{1,2}(?:\.\d{3})|\d{1,4})\s+"
    r"(?:(?:neue[nr]?|weitere[nr]?|moderne[nr]?|öffentlich|frei|finanzierte[nr]?|geförderte[nr]?|"
    r"barrierefreie[nr]?|bezahlbare[nr]?)\s+)*"
    r"(?:Miet|Sozial|Eigentums)?(?:wohnungen|wohneinheiten)\b",
    re.IGNORECASE,
)


def extrahiere_we(text: str) -> tuple[int | None, str | None]:
    """Erste im Text genannte Wohnungszahl samt wörtlichem Zitat (≤ 200 Zeichen) als Beleg.
    Nichts gefunden -> `(None, None)`, nie raten oder hochrechnen."""
    for treffer in _WE_RE.finditer(text):
        zahl = int(treffer.group(1).replace(".", ""))
        if not 2 <= zahl <= 5000:
            continue
        start = max(0, treffer.start() - 80)
        ende = min(len(text), treffer.end() + 80)
        zitat = " ".join(text[start:ende].split())
        if start > 0:
            zitat = "…" + zitat
        if ende < len(text):
            zitat += "…"
        return zahl, zitat[:MAX_EVIDENZ_ZEICHEN]
    return None, None


def normalisiere_url(url: str) -> str:
    """Dedup-Schlüssel: ohne Fragment, Tracking-Parameter und abschließenden Schrägstrich."""
    teile = urlparse(url.strip())
    query = "&".join(p for p in teile.query.split("&") if p and not p.lower().startswith("utm_"))
    pfad = teile.path.rstrip("/") or "/"
    return urlunparse((teile.scheme.lower(), teile.netloc.lower(), pfad, "", query, ""))


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def _domain_ausgeschlossen(domain: str, config: WebRechercheConfig) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in config.ausgeschlossene_domains)


GRUND_FRUEHPHASE = "keine Baugenehmigung und kein Baubeginn im Text (Frühphase)"


def pruefe_ergebnis(
    ergebnis: Suchergebnis, auftrag: Suchauftrag, config: WebRechercheConfig, heute: date | None = None
) -> tuple[WebFund | None, str | None]:
    """`(Fund, None)` oder `(None, Grund)`. Bedingungen: Domain nicht ausgeschlossen, Ort genannt
    (Wortgrenze, sonst passt z. B. "Stade" auch in fremde Wörter), Wohnbau-Begriff vorhanden, Phase 3/4
    erkennbar, kein kleines reines Eigentumsprojekt. Die Phase kommt aus dem Text, nicht aus der
    Suchanfrage - eine Suche nach "Baugenehmigung" findet oft auch Artikel zum Baubeginn."""
    text = f"{ergebnis.titel}\n{ergebnis.inhalt}"
    domain = _domain(ergebnis.url)
    if _domain_ausgeschlossen(domain, config):
        return None, f"Website ausgeschlossen ({domain})"
    if not re.search(rf"\b{re.escape(auftrag.ort.erkennungs_wort)}\b", text, re.IGNORECASE):
        return None, f"Ort {auftrag.ort.erkennungs_wort} nicht genannt"
    if not _enthaelt(text, config.wohn_stichworte):
        return None, "kein Wohnbau"
    phase = klassifiziere_phase(text, config, heute)
    if phase is None:
        return None, GRUND_FRUEHPHASE
    we_anzahl, evidenz = extrahiere_we(text)
    wohnform = bestimme_wohnform(text, config)
    if wohnform == "EIGENTUM" and we_anzahl is not None and we_anzahl < config.eigentum_min_we:
        # kleines reines ETW-Projekt; Großprojekte und Fälle ohne Zahl bleiben drin
        return None, f"reines Eigentumsprojekt mit {we_anzahl} WE (unter {config.eigentum_min_we})"
    return WebFund(
        url=normalisiere_url(ergebnis.url),
        titel=(ergebnis.titel or ergebnis.url)[:300],
        auszug=ergebnis.inhalt[:MAX_AUSZUG_ZEICHEN],
        domain=domain,
        ort=auftrag.ort.name,
        bundesland=auftrag.ort.bundesland,
        entfernung_km=auftrag.entfernung_km,
        phase=phase,
        we_anzahl=we_anzahl,
        evidenz=evidenz,
        suchanfrage=auftrag.anfrage,
        veroeffentlicht=ergebnis.veroeffentlicht,
        wohnform=wohnform,
    ), None


def bewerte_ergebnis(
    ergebnis: Suchergebnis, auftrag: Suchauftrag, config: WebRechercheConfig, heute: date | None = None
) -> WebFund | None:
    """Fund oder `None` (verworfen), siehe `pruefe_ergebnis()` für die Bedingungen und den Grund."""
    return pruefe_ergebnis(ergebnis, auftrag, config, heute)[0]


def als_mail_eintrag(fund: WebFund) -> NeuerWebFund:
    return NeuerWebFund(
        ort=fund.ort,
        phase=PHASE_LABEL[fund.phase],
        titel=fund.titel,
        we_anzahl=fund.we_anzahl,
        entfernung_km=fund.entfernung_km,
        url=fund.url,
    )


def demo_funde(config: WebRechercheConfig) -> list[WebFund]:
    """Die Funde, die ein Demo-Lauf über alle Orte ergäbe (ohne Datenbank, ohne Budget) - für die
    Demo-Datenbank des Dashboards (radar/demo_data.py)."""
    funde = []
    for auftrag in baue_suchauftraege(config):
        for ergebnis in DEMO_SUCHERGEBNISSE.get((auftrag.ort.name, auftrag.phase), []):
            fund = bewerte_ergebnis(ergebnis, auftrag, config)
            if fund is not None:
                funde.append(fund)
    return funde


# -- Lauf -----------------------------------------------------------------------------------------


@dataclass
class WebRechercheErgebnis:
    anfragen: int = 0
    ergebnisse: int = 0
    funde: int = 0
    neue_funde: int = 0
    verworfen: int = 0
    fehler: list[str] = field(default_factory=list)
    abbruch_grund: str | None = None
    demo: bool = False  # Ergebnisse vom DemoAnbieter, keine echte Suche
    neu_fuer_mail: list[NeuerWebFund] = field(default_factory=list)  # für radar/notifier.py
    alle_funde: list[NeuerWebFund] = field(default_factory=list)  # neue + bekannte (Terminal-Ausgabe --test)
    # Echte, geprüfte Projekte (VERIFIZIERTE_DEMO_URLS), die die Bewertung verworfen hat: (Ort, Titel, URL, Grund)
    verworfene_echte: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.abbruch_grund is None and not self.fehler

    def zusammenfassung(self) -> str:
        text = (
            f"{self.anfragen} Suchanfragen, {self.ergebnisse} Ergebnisse, {self.funde} relevante Funde "
            f"({self.neue_funde} neu), {self.verworfen} verworfen, {len(self.fehler)} Fehler"
        )
        if self.abbruch_grund:
            text += f" - ABBRUCH: {self.abbruch_grund}"
        if self.demo:
            text = f"DEMO (Beispieldaten, keine echte Suche, 0 Credits): {text}"
        return text


_sleep = time.sleep  # in Tests ersetzbar


def fuehre_recherche_aus(
    conn: sqlite3.Connection,
    config: WebRechercheConfig,
    anbieter: SuchAnbieter,
    *,
    heute: date | None = None,
) -> WebRechercheErgebnis:
    """Alle Suchaufträge abarbeiten, Funde speichern. Ein Fehler bei einer Anfrage stoppt nie den
    ganzen Lauf (wie bei den Quellen), nur Konto-Fehler und die Fehler-Notbremse tun das."""
    heute_iso = (heute or datetime.now().date()).isoformat()
    kostenpflichtig = getattr(anbieter, "kostenpflichtig", True)
    ergebnis = WebRechercheErgebnis(demo=not kostenpflichtig)
    fehler_in_folge = 0

    for i, auftrag in enumerate(baue_suchauftraege(config)):
        if ergebnis.anfragen >= config.max_anfragen_pro_lauf:
            ergebnis.abbruch_grund = f"Budget pro Lauf erschöpft ({config.max_anfragen_pro_lauf} Anfragen)"
            break
        if kostenpflichtig:
            if database.such_anfragen_am(conn, heute_iso) >= config.max_anfragen_pro_tag:
                ergebnis.abbruch_grund = f"Tageslimit erreicht ({config.max_anfragen_pro_tag} Anfragen)"
                break
            if i > 0 and config.request_delay_seconds:
                _sleep(config.request_delay_seconds)
            database.zaehle_such_anfrage(conn, heute_iso)  # vor dem Senden verbuchen
        ergebnis.anfragen += 1
        try:
            treffer = anbieter.suche(auftrag.anfrage)
        except SucheGesperrt as exc:
            ergebnis.abbruch_grund = str(exc)
            logger.error("Web-Recherche gestoppt: %s", exc)
            break
        except Exception as exc:  # Timeout, 429, 5xx, kaputte Antwort -> nächste Anfrage
            fehler_in_folge += 1
            ergebnis.fehler.append(f"{auftrag.anfrage}: {exc}")
            logger.warning("Suchanfrage '%s' fehlgeschlagen: %s", auftrag.anfrage, exc)
            if fehler_in_folge >= config.max_fehler_in_folge:
                ergebnis.abbruch_grund = f"{fehler_in_folge} Fehler in Folge, zuletzt: {exc}"
                logger.error("Web-Recherche-Notbremse: %s", ergebnis.abbruch_grund)
                break
            continue
        fehler_in_folge = 0

        ergebnis.ergebnisse += len(treffer)
        for t in treffer:
            fund, grund = pruefe_ergebnis(t, auftrag, config)
            if fund is None:
                ergebnis.verworfen += 1
                url = normalisiere_url(t.url)
                if url in VERIFIZIERTE_DEMO_URLS and url not in {v[2] for v in ergebnis.verworfene_echte}:
                    ergebnis.verworfene_echte.append((auftrag.ort.name, t.titel, url, grund or ""))
                continue
            _, ist_neu = database.upsert_web_fund(conn, fund)
            ergebnis.funde += 1
            if fund.url not in {f.url for f in ergebnis.alle_funde}:
                ergebnis.alle_funde.append(als_mail_eintrag(fund))
            if ist_neu:
                ergebnis.neue_funde += 1
                ergebnis.neu_fuer_mail.append(als_mail_eintrag(fund))

    logger.info("Web-Recherche: %s", ergebnis.zusammenfassung())
    return ergebnis


def run_web_recherche(
    settings: Settings | None = None,
    anbieter: SuchAnbieter | None = None,
    *,
    max_orte: int | None = None,
    db_pfad: str | None = None,
    demo: bool = False,
) -> WebRechercheErgebnis:
    """Einstieg für CLI und Dashboard (`python -m radar web-recherche`). `max_orte` beschränkt die
    Suche auf die nächstgelegenen Orte (Testsuche mit wenigen Credits), `db_pfad` schreibt in eine
    andere Datenbank als `settings.database.path` (z. B. eine Testdatenbank).

    `demo=True` nutzt den `DemoAnbieter` statt der Such-API (kein Key, keine Credits). Ohne
    `db_pfad` landen die Beispieldaten in der Demo-Datenbank des Dashboards (data/demo/demo.db,
    sichtbar mit "Demo-Daten anzeigen"); in die echte Datenbank schreibt ein Demo-Lauf nie."""
    settings = settings or load_settings()
    produktiv_db = settings.resolve_path(settings.database.path).resolve()
    if demo and db_pfad is None:
        from radar.demo_data import demo_db_path  # hier importiert: demo_data importiert dieses Modul

        db_pfad = str(demo_db_path(settings))
    if db_pfad is not None:
        settings = replace(settings, database=DatabaseConfig(path=db_pfad))
    if demo and settings.resolve_path(settings.database.path).resolve() == produktiv_db:
        raise ValueError(f"Demo-Daten werden nie in die echte Datenbank geschrieben ({produktiv_db})")
    config = load_web_config(settings)
    if max_orte is not None:
        naechste = {ort.name for ort, _ in orte_im_radius(config)[:max_orte]}
        config = replace(config, orte=[o for o in config.orte if o.name in naechste])
    if anbieter is None:
        if not demo and not settings.betrieb.live_api_aufrufe:
            raise LiveAufrufeDeaktiviert(LIVE_AUS_HINWEIS)  # letzte Sicherung, falls ein Aufrufer es übersieht
        anbieter = DemoAnbieter(config) if demo else TavilyClient(tavily_api_key(), config)
    logger.info(
        "Web-Recherche: %d Orte im Umkreis von %.0f km um %s",
        len(orte_im_radius(config)),
        config.radius_km,
        config.zentrum_name,
    )
    logger.info("Ziel-Datenbank: %s", settings.resolve_path(settings.database.path))
    with closing(database.connect(settings)) as conn:
        return fuehre_recherche_aus(conn, config, anbieter)
