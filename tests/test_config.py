import re

import pytest

from radar.config import FiltersConfig, RegionConfig, Source, enabled_sources, load_settings, load_sources


def test_default_settings_load():
    s = load_settings()
    assert s.filters.min_units == 6
    assert s.filters.prefilter_keywords
    assert s.filters.default_committee_patterns
    assert s.scraper.request_delay_seconds >= 1
    assert "Neu" in s.dashboard.statuses


def test_all_target_regions_configured():
    s = load_settings()
    assert set(s.regions) == {"SH", "HH", "NI", "HB", "MV", "BE"}


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        FiltersConfig(unknown_units_policy="ignore")


def test_invalid_regex_rejected():
    with pytest.raises(re.error):
        FiltersConfig(default_committee_patterns=["(unclosed"])


def test_secrets_are_required_at_use_time(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCRAPER_CONTACT", raising=False)
    s = load_settings()
    # load_settings() liest .env neu ein (python-dotenv); auf Entwicklermaschinen mit echter
    # .env-Datei (SCRAPER_CONTACT gesetzt) muss hier erneut geleert werden, sonst testet dieser
    # Test nichts mehr.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCRAPER_CONTACT", raising=False)
    with pytest.raises(RuntimeError):
        _ = s.anthropic_api_key
    with pytest.raises(RuntimeError):
        _ = s.user_agent
    monkeypatch.setenv("SCRAPER_CONTACT", "it@example.com")
    assert "it@example.com" in s.user_agent


def _src(**kw):
    base = dict(id="test", name="Test", bundesland="BE", system="allris", base_url="https://example.org/bi/")
    base.update(kw)
    return Source(**base)


def test_min_units_precedence_source_over_region_over_global():
    s = load_settings()
    assert s.min_units_for(_src()) == 6                      # global
    s.regions["BE"] = RegionConfig(name="Berlin", tier="C", min_units=12)
    assert s.min_units_for(_src()) == 12                     # Region
    assert s.min_units_for(_src(min_units=20)) == 20         # Quelle


def test_tier_precedence():
    s = load_settings()
    assert s.tier_for(_src(bundesland="HH")) == "A"
    assert s.tier_for(_src(bundesland="HH", tier="C")) == "C"


def test_committee_patterns_source_overrides_default():
    s = load_settings()
    assert s.committee_patterns_for(_src()) == s.filters.default_committee_patterns
    assert s.committee_patterns_for(_src(committees=["^Bauausschuss$"])) == ["^Bauausschuss$"]


def test_source_validation():
    with pytest.raises(ValueError):
        _src(bundesland="NW")                               # nicht im Zielgebiet
    with pytest.raises(ValueError):
        _src(base_url="http://example.org/")                # nur https
    with pytest.raises(ValueError):
        _src(enabled=True, status="candidate")              # nur verifizierte Quellen aktivierbar
    with pytest.raises(ValueError):
        _src(enabled=True, status="blocked")                # blocked ist nie aktivierbar
    with pytest.raises(ValueError):
        _src(enabled=True, status="verified", system="unknown")
    with pytest.raises(ValueError):
        _src(id="Ungültig ID")
    with pytest.raises(ValueError):
        _src(ignore_robots_txt=True)                        # braucht eine Begründung in notes
    with pytest.raises(ValueError):
        _src(request_delay_seconds=-1)
    with pytest.raises(ValueError):
        _src(additional_hosts=["https://example.org"])  # nur reine Hostnamen, keine URLs


def test_source_blocked_status_is_valid_when_disabled():
    s = _src(status="blocked")
    assert s.status == "blocked"
    assert s.enabled is False


def test_source_ignore_robots_txt_with_justification_is_valid():
    s = _src(ignore_robots_txt=True, notes="Begründung: siehe CLAUDE.md")
    assert s.ignore_robots_txt is True


def test_request_delay_seconds_precedence():
    settings = load_settings()
    assert settings.request_delay_seconds_for(_src()) == settings.scraper.request_delay_seconds
    assert settings.request_delay_seconds_for(_src(request_delay_seconds=10.0)) == 10.0


def test_default_sources_valid():
    s = load_settings()
    sources = load_sources(s)
    ids = [x.id for x in sources]
    assert len(ids) == len(set(ids))
    norderstedt = next(x for x in sources if x.id == "norderstedt")
    assert norderstedt.system == "sessionnet" and norderstedt.enabled and norderstedt.bundesland == "SH"
    assert norderstedt.options["calendar_page"] == "si0040.php"
    assert norderstedt.host == "buergerinfo.norderstedt.de"
    active = enabled_sources(sources, s)
    assert norderstedt in active
    assert all(x.status == "verified" for x in active)
