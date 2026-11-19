"""Tests für radar/sources/rss.py gegen zwei echte, am 20.11.2026 einmalig abgerufene Feed-Seiten von noa4.de
(tests/fixtures/noa4/). Kein Netzzugriff."""

from datetime import date
from pathlib import Path

import pytest
import requests

from radar.config import load_settings, load_sources
from radar.sources.base import SourceLayoutError
from radar.sources.registry import get_adapter
from radar.sources.rss import RssAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "noa4"
SEITE_1 = (FIXTURES / "feed_lokal_1.xml").read_text(encoding="utf-8")
SEITE_2 = (FIXTURES / "feed_lokal_2.xml").read_text(encoding="utf-8")

# Nach dem Muster des echten Artikels vom 06.08.2026 (Liesel-Hünichen-Weg), gekürzt
WOHNBAU_ITEM = """<item>
  <title>Spatenstich für 69 geförderte Wohnungen in Norderstedt</title>
  <link>https://www.noa4.de/spatenstich-fuer-69-gefoerderte-wohnungen-in-norderstedt/</link>
  <pubDate>Thu, 06 Aug 2026 09:00:00 +0000</pubDate>
  <category><![CDATA[Lokal]]></category><category><![CDATA[Norderstedt]]></category>
  <guid isPermaLink="false">https://www.noa4.de/?p=14000</guid>
  <description><![CDATA[Die Baugenossenschaft baut im Liesel-Hünichen-Weg.
<p><a href="https://www.noa4.de/spatenstich-fuer-69-gefoerderte-wohnungen-in-norderstedt/">Source</a></p>]]></description>
</item>"""


def _feed(*items: str) -> str:
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>noa4</title>{"".join(items)}</channel></rss>'


class _Antwort:
    def __init__(self, text):
        self.text = text


class _FakeHttp:
    def __init__(self, seiten: dict[str, str]):
        self._seiten = seiten
        self.abrufe: list[str] = []

    def get(self, url):
        self.abrufe.append(url)
        if url not in self._seiten:
            raise requests.HTTPError(f"404 {url}")
        return _Antwort(self._seiten[url])


@pytest.fixture(scope="module")
def quelle():
    return next(q for q in load_sources(load_settings()) if q.id == "noa4")


def test_quelle_ist_aktiv_und_registriert(quelle):
    assert quelle.status == "verified" and quelle.enabled and quelle.system == "rss"
    assert isinstance(get_adapter(quelle, _FakeHttp({})), RssAdapter)
    # \b muss als Wortgrenze ankommen, nicht als Steuerzeichen (YAML in einfachen Anführungszeichen)
    assert quelle.options["ort_muster"] == ["\\bNorderstedt"]


def test_echter_feed_wird_gelesen(quelle):
    artikel = RssAdapter(quelle).parse_feed(SEITE_1)
    assert len(artikel) == 10
    erster = artikel[0]
    assert erster.artikel_id == "15780"
    assert erster.datum == date(2026, 11, 19)
    assert erster.url.startswith("https://www.noa4.de/")
    assert "Norderstedt" in erster.kategorien
    assert not erster.anriss.endswith("Source")  # angehängter Feed-Link entfernt


def test_echte_artikel_ohne_wohnbau_werden_nicht_gelistet(quelle):
    adapter = RssAdapter(quelle, _FakeHttp({"category/lokal/feed/": SEITE_1, "category/lokal/feed/?paged=2": SEITE_2}))
    assert adapter.list_sessions(date(2026, 10, 1), date(2026, 12, 31)) == []  # Blaulicht, Haushalt, Schule ...


def test_wohnbau_artikel_wird_sitzung_mit_textdokument(quelle):
    adapter = RssAdapter(quelle, _FakeHttp({"category/lokal/feed/": _feed(WOHNBAU_ITEM)}))
    (sitzung,) = adapter.list_sessions(date(2026, 8, 1), date(2026, 8, 31))
    assert (sitzung.external_id, sitzung.gremium, sitzung.datum) == ("14000", "Presseartikel", date(2026, 8, 6))
    (dok,) = adapter.list_documents(sitzung)
    assert dok.url == sitzung.url and dok.vorlage_external_id == "artikel-14000"
    assert "69 geförderte Wohnungen" in dok.text and "Liesel-Hünichen-Weg" in dok.text  # kein Download nötig


def test_ortsfilter_greift(quelle):
    hamburg = WOHNBAU_ITEM.replace("in Norderstedt", "in Hamburg").replace("Norderstedt]]", "Hamburg]]")
    adapter = RssAdapter(quelle, _FakeHttp({"category/lokal/feed/": _feed(hamburg)}))
    assert adapter.list_sessions(date(2026, 8, 1), date(2026, 8, 31)) == []


def test_blaettert_bis_zum_fensterbeginn_und_stoppt_bei_404(quelle):
    http = _FakeHttp({"category/lokal/feed/": SEITE_1, "category/lokal/feed/?paged=2": SEITE_2})
    RssAdapter(quelle, http).list_sessions(date(2026, 11, 10), date(2026, 12, 31))
    assert http.abrufe == ["category/lokal/feed/"]  # Seite 1 reicht schon bis vor den 10.11.

    http = _FakeHttp({"category/lokal/feed/": SEITE_1, "category/lokal/feed/?paged=2": SEITE_2})
    RssAdapter(quelle, http).list_sessions(date(2026, 1, 1), date(2026, 12, 31))
    assert http.abrufe[-1] == "category/lokal/feed/?paged=3"  # 404 -> Ende, kein Fehler


def test_fail_loud_bei_leerer_erster_seite(quelle):
    adapter = RssAdapter(quelle, _FakeHttp({"category/lokal/feed/": _feed()}))
    with pytest.raises(SourceLayoutError):
        adapter.list_sessions(date(2026, 1, 1), date(2026, 12, 31))


def test_feed_url_ist_pflicht(quelle):
    from dataclasses import replace

    with pytest.raises(ValueError):
        RssAdapter(replace(quelle, options={}))


def test_seitenlimit_meldet_nur_den_gelesenen_zeitraum(quelle):
    from dataclasses import replace

    q = replace(quelle, options={**quelle.options, "max_seiten": 1})
    http = _FakeHttp({"category/lokal/feed/": SEITE_1, "category/lokal/feed/?paged=2": SEITE_2})
    adapter = RssAdapter(q, http)
    adapter.list_sessions(date(2024, 1, 1), date(2026, 12, 31))
    assert adapter.abgedeckt_ab == date(2026, 11, 6)  # ältester Artikel auf Seite 1, nicht der 01.01.2024

    adapter = RssAdapter(quelle, _FakeHttp({"category/lokal/feed/": SEITE_1, "category/lokal/feed/?paged=2": SEITE_2}))
    adapter.list_sessions(date(2024, 1, 1), date(2026, 12, 31))
    assert adapter.abgedeckt_ab == date(2024, 1, 1)  # Feed-Ende (404) erreicht -> vollständig


def test_scraper_vermerkt_nur_abgedeckten_zeitraum(monkeypatch, tmp_path, quelle):
    from dataclasses import replace

    from radar import database
    from radar.config import BetriebConfig, DatabaseConfig
    from radar.scraper import run_full
    from tests.conftest import FakeAnthropicClient

    monkeypatch.setenv("SCRAPER_CONTACT", "test@example.com")
    settings = replace(
        load_settings(),
        database=DatabaseConfig(path=str(tmp_path / "radar.db")),
        llm=replace(load_settings().llm, cache_dir=str(tmp_path / "cache")),
        betrieb=BetriebConfig(live_api_aufrufe=False),
    )
    q = replace(quelle, options={**quelle.options, "max_seiten": 1})
    http = _FakeHttp({"category/lokal/feed/": SEITE_1})
    monkeypatch.setattr("radar.scraper.load_sources", lambda s: [q])
    monkeypatch.setattr("radar.scraper.HttpClient", lambda src, s: http)
    run_full(settings, client=FakeAnthropicClient([]))
    conn = database.connect(settings)
    try:
        assert database.quellen_zeitraum(conn, "noa4")[0] == date(2026, 11, 6)
    finally:
        conn.close()
