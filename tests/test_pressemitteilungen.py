"""Tests für radar/sources/pressemitteilungen.py gegen die echte, am 20.11.2026 einmalig abgerufene Listenseite der
Stadt Norderstedt (tests/fixtures/norderstedt_presse/pressestelle.html). Kein Netzzugriff."""

from datetime import date
from pathlib import Path

import pytest

from radar.config import load_settings, load_sources
from radar.sources.base import SourceLayoutError
from radar.sources.pressemitteilungen import PressemitteilungenAdapter
from radar.sources.registry import get_adapter

FIXTURE = Path(__file__).parent / "fixtures" / "norderstedt_presse" / "pressestelle.html"


class _Antwort:
    def __init__(self, text):
        self.text = text


class _FakeHttp:
    def __init__(self, text):
        self._text = text
        self.abrufe: list[str] = []

    def get(self, url):
        self.abrufe.append(url)
        return _Antwort(self._text)


@pytest.fixture(scope="module")
def quelle():
    return next(q for q in load_sources(load_settings()) if q.id == "norderstedt_presse")


@pytest.fixture
def adapter(quelle):
    return PressemitteilungenAdapter(quelle, _FakeHttp(FIXTURE.read_text(encoding="iso-8859-15")))


def test_quelle_ist_aktiv_und_registriert(quelle):
    assert quelle.status == "verified" and quelle.enabled
    assert isinstance(get_adapter(quelle, _FakeHttp("")), PressemitteilungenAdapter)
    assert load_settings().committee_patterns_for(quelle) == ["^Pressemitteilung$"]


def test_titelfilter_waehlt_nur_wohnbau_meldungen(adapter):
    meldungen = adapter.parse_liste(FIXTURE.read_text(encoding="iso-8859-15"))
    assert len(meldungen) == 24  # von 736 Meldungen
    titel = [t for t, _, _ in meldungen.values()]
    assert any("Neuer Wohnraum in Norderstedt-Garstedt" in t for t in titel)
    assert any("B-Plan 337" in t for t in titel)
    # Wortgrenze: "früher als gewohnt" (Abfallentsorgung) darf nicht treffen
    assert not any("Abfallentsorgung" in t for t in titel)


def test_datum_aus_zeitstempel_im_link(adapter):
    meldungen = adapter.parse_liste(FIXTURE.read_text(encoding="iso-8859-15"))
    titel, url, datum = meldungen["14991"]
    assert datum == date(2026, 11, 13)
    assert url.startswith("/media/custom/3224_14991_1.PDF?")


def test_sitzungen_im_zeitraum_und_ein_dokument_je_meldung(adapter):
    sitzungen = adapter.list_sessions(date(2024, 1, 1), date(2026, 12, 31))
    assert len(sitzungen) == 17  # die Treffer ab 2024
    assert {s.gremium for s in sitzungen} == {"Pressemitteilung"}
    assert len(adapter.http.abrufe) == 1  # Listenseite nur einmal je Lauf

    ohepark = next(s for s in sitzungen if s.external_id == "9563")
    (dok,) = adapter.list_documents(ohepark)
    assert dok.vorlage_external_id == "pm-9563" and dok.vorlage_nr == "PM 9563"
    assert "Ohechaussee" in dok.titel
    assert dok.url == ohepark.url
    assert len(adapter.http.abrufe) == 1


def test_doppelt_gelistete_meldung_nur_einmal(quelle):
    html = (
        '<a href="/media/custom/3224_1_1.PDF?1794565590">B-Plan 1 liegt aus</a>'
        '<a href="/media/custom/3224_1_1.PDF?1794565590">B-Plan 1 liegt aus</a>'
        '<a href="/media/custom/3224_2_1.PDF?1794565590"></a>'  # Bild-Link ohne Text
    )
    assert list(PressemitteilungenAdapter(quelle).parse_liste(html)) == ["1"]


def test_fail_loud_ohne_einen_einzigen_link(quelle):
    with pytest.raises(SourceLayoutError):
        PressemitteilungenAdapter(quelle).parse_liste("<html><body>Wartungsarbeiten</body></html>")


def test_list_page_ist_pflicht(quelle):
    from dataclasses import replace

    with pytest.raises(ValueError):
        PressemitteilungenAdapter(replace(quelle, options={}))
