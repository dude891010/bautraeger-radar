"""Tests für den gemeinsamen HTTP-Client (radar/http_client.py): Host-Bindung, robots.txt,
Ratenbegrenzung pro Host, PDF-Validierung und -Cache. Kein Netzzugriff (`requests_mock`)."""

import time

import pytest

from radar.config import (
    DashboardConfig,
    DatabaseConfig,
    FiltersConfig,
    LLMConfig,
    ScanConfig,
    ScraperConfig,
    Settings,
    Source,
)
from radar.http_client import DownloadError, HttpClient, RobotsDisallowedError

BASE_URL = "https://buergerinfo.norderstedt.de/ratsinfo/sessionnet/buergerinfo/"
MINIMAL_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    return Settings(
        scan=ScanConfig(),
        scraper=ScraperConfig(request_delay_seconds=0.0, raw_cache_dir=str(tmp_path / "raw"), max_pdf_size_mb=1),
        filters=FiltersConfig(),
        llm=LLMConfig(),
        database=DatabaseConfig(),
        dashboard=DashboardConfig(),
    )


@pytest.fixture
def source():
    return Source(
        id="norderstedt",
        name="Stadt Norderstedt",
        bundesland="SH",
        system="sessionnet",
        base_url=BASE_URL,
        status="verified",
        enabled=True,
    )


def _allow_robots(requests_mock):
    requests_mock.get("https://buergerinfo.norderstedt.de/robots.txt", status_code=404)


def test_user_agent_contains_contact(requests_mock, source, settings):
    _allow_robots(requests_mock)
    client = HttpClient(source, settings)
    assert "test@example.com" in client.session.headers["User-Agent"]


def test_get_rejects_url_on_different_host(requests_mock, source, settings):
    _allow_robots(requests_mock)
    client = HttpClient(source, settings)
    with pytest.raises(DownloadError):
        client.get("https://evil.example.org/x")


def test_get_resolves_relative_url_against_base(requests_mock, source, settings):
    _allow_robots(requests_mock)
    requests_mock.get(BASE_URL + "info.php", text="ok")
    client = HttpClient(source, settings)
    response = client.get("info.php")
    assert response.text == "ok"


def test_robots_disallow_blocks_request(requests_mock, source, settings):
    requests_mock.get(
        "https://buergerinfo.norderstedt.de/robots.txt",
        text="User-agent: *\nDisallow: /ratsinfo/sessionnet/buergerinfo/gesperrt.php\n",
    )
    client = HttpClient(source, settings)
    with pytest.raises(RobotsDisallowedError):
        client.get("gesperrt.php")


def test_robots_disallow_understands_wildcard_and_end_anchor(requests_mock, source, settings):
    """`urllib.robotparser` (Python-Standardbibliothek) versteht `*`/`$`-Wildcards nicht und hätte
    das hier gefundene, real verbreitete Muster fälschlich als erlaubt behandelt - deshalb
    `protego` (siehe CLAUDE.md, Abschnitt Referenzquelle OParl)."""
    requests_mock.get(
        "https://buergerinfo.norderstedt.de/robots.txt",
        text="User-agent: *\nDisallow: /*.pdf$\n",
    )
    client = HttpClient(source, settings)
    with pytest.raises(RobotsDisallowedError):
        client.get("dokument.pdf")


def test_robots_allows_unlisted_path(requests_mock, source, settings):
    requests_mock.get(
        "https://buergerinfo.norderstedt.de/robots.txt",
        text="User-agent: *\nDisallow: /ratsinfo/sessionnet/buergerinfo/gesperrt.php\n",
    )
    requests_mock.get(BASE_URL + "info.php", text="ok")
    client = HttpClient(source, settings)
    assert client.get("info.php").text == "ok"


def test_ignore_robots_txt_skips_robots_check_for_that_source(requests_mock, source, settings):
    """Ausnahme für Quellen mit `ignore_robots_txt: true` (z. B. transparenz.hamburg.de: sperrt
    /api/ pauschal, dokumentiert und lizenziert denselben Pfad aber selbst ausdrücklich zur
    automatisierten Nutzung, siehe config/sources.yaml). Kein robots.txt-Abruf, keine Sperre."""
    from dataclasses import replace

    src = replace(source, ignore_robots_txt=True, notes="Begründung siehe CLAUDE.md")
    requests_mock.get(BASE_URL + "gesperrt.php", text="ok")
    client = HttpClient(src, settings)
    assert client.get("gesperrt.php").text == "ok"
    assert not any(req.url.endswith("/robots.txt") for req in requests_mock.request_history)


def test_source_can_override_request_delay(requests_mock, source, settings):
    from dataclasses import replace

    _allow_robots(requests_mock)
    requests_mock.get(BASE_URL + "a.php", text="a")
    requests_mock.get(BASE_URL + "b.php", text="b")
    src = replace(source, request_delay_seconds=0.2)
    client = HttpClient(src, settings)  # settings.scraper.request_delay_seconds bleibt 0.0
    client.get("a.php")
    start = time.monotonic()
    client.get("b.php")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


def test_additional_hosts_are_accessible(requests_mock, source, settings):
    """Manche offiziellen Open-Data-Ökosysteme verteilen API und Dateiablage auf verschiedene
    Domains (z. B. Hamburg: API unter suche.transparenz.hamburg.de, PDFs unter daten-hamburg.de) -
    dafür ist `additional_hosts` da."""
    from dataclasses import replace

    src = replace(source, additional_hosts=["dateien.example.org"])
    _allow_robots(requests_mock)
    requests_mock.get("https://dateien.example.org/robots.txt", status_code=404)
    requests_mock.get("https://dateien.example.org/dokument.pdf", text="ok")
    client = HttpClient(src, settings)
    assert client.get("https://dateien.example.org/dokument.pdf").text == "ok"


def test_additional_host_robots_txt_is_checked_independently(requests_mock, source, settings):
    """`ignore_robots_txt` gilt nur für den primären Host, nie für `additional_hosts` - die werden
    immer strikt geprüft."""
    from dataclasses import replace

    src = replace(source, additional_hosts=["dateien.example.org"])
    _allow_robots(requests_mock)
    requests_mock.get(
        "https://dateien.example.org/robots.txt", text="User-agent: *\nDisallow: /gesperrt.pdf\n"
    )
    client = HttpClient(src, settings)
    with pytest.raises(RobotsDisallowedError):
        client.get("https://dateien.example.org/gesperrt.pdf")


def test_unapproved_host_is_still_rejected(requests_mock, source, settings):
    from dataclasses import replace

    src = replace(source, additional_hosts=["dateien.example.org"])
    _allow_robots(requests_mock)
    client = HttpClient(src, settings)
    with pytest.raises(DownloadError):
        client.get("https://evil.example.org/x")


def test_requests_to_same_host_are_throttled(requests_mock, source, settings):
    _allow_robots(requests_mock)
    requests_mock.get(BASE_URL + "a.php", text="a")
    requests_mock.get(BASE_URL + "b.php", text="b")
    settings.scraper.request_delay_seconds = 0.2
    client = HttpClient(source, settings)
    client.get("a.php")
    start = time.monotonic()
    client.get("b.php")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15  # etwas Toleranz gegenüber der eingestellten Mindestpause


def test_download_pdf_accepts_valid_pdf_and_caches_it(requests_mock, source, settings):
    _allow_robots(requests_mock)
    requests_mock.get(BASE_URL + "getfile.php?id=1", content=MINIMAL_PDF, headers={"Content-Type": "application/pdf"})
    client = HttpClient(source, settings)

    path = client.download_pdf("getfile.php?id=1", external_id="1")
    assert path.exists()
    assert path.read_bytes() == MINIMAL_PDF
    assert path.parent.name == "norderstedt"

    # zweiter Aufruf: kein weiterer HTTP-Request nötig (Cache-Treffer)
    requests_mock.get(BASE_URL + "getfile.php?id=1", content=b"sollte nicht abgerufen werden")
    path2 = client.download_pdf("getfile.php?id=1", external_id="1")
    assert path2 == path
    assert path2.read_bytes() == MINIMAL_PDF


def test_download_pdf_rejects_non_pdf_content_type(requests_mock, source, settings):
    _allow_robots(requests_mock)
    requests_mock.get(
        BASE_URL + "getfile.php?id=2", content=b"<html>oops</html>", headers={"Content-Type": "text/html"}
    )
    client = HttpClient(source, settings)
    with pytest.raises(DownloadError):
        client.download_pdf("getfile.php?id=2", external_id="2")


def test_download_pdf_rejects_wrong_magic_bytes(requests_mock, source, settings):
    _allow_robots(requests_mock)
    requests_mock.get(
        BASE_URL + "getfile.php?id=3", content=b"NOTAPDF...", headers={"Content-Type": "application/pdf"}
    )
    client = HttpClient(source, settings)
    with pytest.raises(DownloadError):
        client.download_pdf("getfile.php?id=3", external_id="3")


def test_download_pdf_rejects_oversized_content(requests_mock, source, settings):
    _allow_robots(requests_mock)
    too_big = MINIMAL_PDF + b"0" * (2 * 1024 * 1024)  # > max_pdf_size_mb=1
    requests_mock.get(BASE_URL + "getfile.php?id=4", content=too_big, headers={"Content-Type": "application/pdf"})
    client = HttpClient(source, settings)
    with pytest.raises(DownloadError):
        client.download_pdf("getfile.php?id=4", external_id="4")


def test_download_pdf_sanitizes_path_traversal_in_external_id(requests_mock, source, settings, tmp_path):
    _allow_robots(requests_mock)
    requests_mock.get(BASE_URL + "getfile.php?id=5", content=MINIMAL_PDF, headers={"Content-Type": "application/pdf"})
    client = HttpClient(source, settings)

    path = client.download_pdf("getfile.php?id=5", external_id="../../etc/passwd")

    cache_root = (tmp_path / "raw").resolve()
    assert cache_root in path.resolve().parents  # kein Ausbruch aus dem Cache-Verzeichnis
    assert "/" not in path.name and ".." not in path.name


def test_download_pdf_rejects_empty_external_id(requests_mock, source, settings):
    _allow_robots(requests_mock)
    client = HttpClient(source, settings)
    with pytest.raises(DownloadError):
        client.download_pdf("getfile.php?id=6", external_id="///")
