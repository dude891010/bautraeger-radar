"""Gemeinsamer HTTP-Client für alle Quellen (Schritt 2).

Ein Client pro Quelle und Lauf: eine `requests.Session` (Cookies bleiben über mehrere Abrufe
erhalten, z. B. für die SessionNet-Anmeldung), Retry mit Backoff für 429/5xx, feste Timeouts,
ein identifizierbarer User-Agent, robots.txt-Prüfung und eine Mindestpause zwischen Requests an
denselben Host. Adapter kennen nur diese Schnittstelle (`get`, `download_pdf`), keine
Netzwerkdetails (siehe CLAUDE.md, Abschnitt "Scraping-Regeln").
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from protego import Protego
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from radar.config import Settings, Source

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"
MAX_WEITERLEITUNGEN = 5

# Ratenbegrenzung ist Prozess-weit pro Host, nicht nur pro Client-Instanz: mehrere Quellen mit
# demselben Host (z. B. ein gemeinsames Amts-SessionNet mehrerer Gemeinden) dürfen sich nicht
# gegenseitig überholen.
_host_locks: dict[str, threading.Lock] = {}
_host_last_request: dict[str, float] = {}
_locks_guard = threading.Lock()


class DownloadError(RuntimeError):
    """Download verweigert oder ungültig (falscher Host, zu groß, kein PDF, ...)."""


class RobotsDisallowedError(DownloadError):
    """robots.txt verbietet diesen Pfad für unseren User-Agent."""


def _lock_for(host: str) -> threading.Lock:
    with _locks_guard:
        return _host_locks.setdefault(host, threading.Lock())


def _safe_filename(external_id: str) -> str:
    """Aus einer externen ID einen sicheren Dateinamensteil machen (kein Path-Traversal)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", external_id).strip("_")
    if not safe:
        raise DownloadError(f"Ungültige externe ID für Dateinamen: {external_id!r}")
    return safe


def _load_robots(session: requests.Session, base_url: str, timeout: tuple[float, float]) -> Protego:
    """`Protego` statt der Standardbibliothek `urllib.robotparser`: Letztere versteht die
    gängigen `*`/`$`-Wildcards (z. B. `Disallow: /*.pdf$`) nicht und hätte einen so gesperrten
    Pfad fälschlich als erlaubt behandelt (gefunden bei der Prüfung eines echten OParl-Anbieters,
    siehe CLAUDE.md)."""
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        response = session.get(robots_url, timeout=timeout)
        if response.status_code == 200:
            return Protego.parse(response.text)
        return Protego.parse("")  # keine robots.txt -> nichts explizit verboten
    except requests.RequestException:
        logger.warning("robots.txt nicht erreichbar (%s), Abruf wird deswegen nicht blockiert", robots_url)
        return Protego.parse("")


class HttpClient:
    """Ein Client pro Quelle. `session` hält Cookies über die Lebensdauer des Laufs."""

    def __init__(self, source: Source, settings: Settings):
        self.source = source
        self.settings = settings
        self.host = source.host  # "primärer" Host (base_url); weitere: source.additional_hosts
        self.hosts = {source.host, *source.additional_hosts}

        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings.user_agent
        retry = Retry(
            total=settings.scraper.max_retries,
            backoff_factor=settings.scraper.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._timeout = (settings.scraper.connect_timeout_seconds, settings.scraper.read_timeout_seconds)
        # Pro Host eine eigene robots.txt (siehe `additional_hosts`): der primäre Host wird wie
        # bisher sofort geladen (respektiert `ignore_robots_txt`), zusätzliche Hosts werden erst
        # bei Bedarf und immer strikt geprüft - `ignore_robots_txt` gilt nur für den primären Host.
        self._robots_by_host: dict[str, Protego | None] = {}
        if settings.scraper.respect_robots_txt and not source.ignore_robots_txt:
            self._robots_by_host[source.host] = _load_robots(self.session, source.base_url, self._timeout)
        else:
            self._robots_by_host[source.host] = None

    # -- Zugriffskontrolle ----------------------------------------------------------------------
    def _check_host(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise DownloadError(f"Schema '{parsed.scheme}' nicht erlaubt: {url}")
        host = parsed.hostname
        if host not in self.hosts:
            raise DownloadError(
                f"Host '{host}' ist für Quelle '{self.source.id}' nicht freigegeben (erlaubt: {sorted(self.hosts)})"
            )

    def _robots_for(self, host: str) -> Protego | None:
        if host not in self._robots_by_host:
            if not self.settings.scraper.respect_robots_txt:
                self._robots_by_host[host] = None
            else:
                self._robots_by_host[host] = _load_robots(self.session, f"https://{host}/", self._timeout)
        return self._robots_by_host[host]

    def _check_robots(self, url: str) -> None:
        robots = self._robots_for(urlparse(url).hostname)
        if robots is not None and not robots.can_fetch(url, self.settings.user_agent):
            raise RobotsDisallowedError(f"robots.txt verbietet den Abruf von {url}")

    def _throttle(self, host: str) -> None:
        """Mindestens `request_delay_seconds` Abstand zum letzten Request an denselben Host."""
        lock = _lock_for(host)
        with lock:
            last = _host_last_request.get(host)
            delay = self.settings.request_delay_seconds_for(self.source)
            if last is not None:
                wait = delay - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
            _host_last_request[host] = time.monotonic()

    # -- HTML/JSON --------------------------------------------------------------------------------
    def get(self, url: str, **kwargs) -> requests.Response:
        """GET mit Host-/robots.txt-Prüfung, Ratenbegrenzung und Timeout.

        `url` darf relativ zur `base_url` der Quelle sein (üblich für SessionNet-Seitennamen) oder
        absolut auf einem der `additional_hosts` liegen."""
        response = self._get_geprueft(urljoin(self.source.base_url, url), **kwargs)
        response.raise_for_status()
        return response

    def _get_geprueft(self, url: str, **kwargs) -> requests.Response:
        """GET, das Weiterleitungen selbst verfolgt und **jedes** Ziel wie die Ausgangs-URL prüft
        (Host-Freigabe, robots.txt, Pause). `requests` folgt Weiterleitungen sonst automatisch und
        ungeprüft - eine manipulierte oder kompromittierte Quelle könnte den Scraper so per
        `302 Location: http://intranet/...` auf beliebige, auch interne Adressen umlenken (SSRF)."""
        kwargs.setdefault("timeout", self._timeout)
        for _ in range(MAX_WEITERLEITUNGEN + 1):
            self._check_host(url)
            self._check_robots(url)
            self._throttle(urlparse(url).hostname)
            response = self.session.get(url, allow_redirects=False, **kwargs)
            if not response.is_redirect:
                return response
            ziel = urljoin(url, response.headers["Location"])
            response.close()
            logger.debug("Weiterleitung %s -> %s", url, ziel)
            url = ziel
        raise DownloadError(f"Mehr als {MAX_WEITERLEITUNGEN} Weiterleitungen, abgebrochen bei {url}")

    # -- PDF-Download mit Cache ------------------------------------------------------------------
    def download_pdf(self, url: str, external_id: str) -> Path:
        """PDF herunterladen, validieren (Content-Type, Größe, Magic-Bytes) und cachen unter
        `data/raw/<quelle_id>/<external_id>--<sha256-kurz>.pdf`.

        Liegt für diese `external_id` bereits eine Datei im Cache, wird nicht erneut
        heruntergeladen (idempotenter, inkrementeller Lauf). Ändert sich eine Anlage auf der
        Quelle unbemerkt, greift die Hash-Prüfung in der Datenbank (Schritt 4), die bei Bedarf
        den Cache-Eintrag verwirft und einen erneuten Download erzwingt.
        """
        cache_dir = self.settings.resolve_path(self.settings.scraper.raw_cache_dir) / self.source.id
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_id = _safe_filename(external_id)
        existing = sorted(cache_dir.glob(f"{safe_id}--*.pdf"))
        if existing:
            logger.debug("PDF bereits im Cache, kein erneuter Download: %s", existing[0])
            return existing[0]

        full_url = urljoin(self.source.base_url, url)
        max_bytes = self.settings.scraper.max_pdf_size_mb * 1024 * 1024
        with self._get_geprueft(full_url, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "pdf" not in content_type.lower():
                raise DownloadError(f"{full_url}: unerwarteter Content-Type '{content_type}' (kein PDF)")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise DownloadError(
                    f"{full_url}: Content-Length {content_length} überschreitet Limit ({max_bytes} Bytes)"
                )

            hasher = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError(f"{full_url}: Download überschreitet Limit ({max_bytes} Bytes)")
                hasher.update(chunk)
                chunks.append(chunk)

        content = b"".join(chunks)
        if not content.startswith(PDF_MAGIC):
            raise DownloadError(f"{full_url}: Inhalt beginnt nicht mit PDF-Magic-Bytes (%PDF)")

        digest = hasher.hexdigest()[:16]
        target = cache_dir / f"{safe_id}--{digest}.pdf"
        target.write_bytes(content)
        logger.info("PDF gecacht: %s (%d Bytes)", target, total)
        return target
