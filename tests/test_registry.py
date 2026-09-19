import pytest

from radar.config import Source, load_settings, load_sources
from radar.sources.base import SourceAdapter
from radar.sources.registry import ADAPTERS, UnknownSystemError, get_adapter


def _src(system):
    return Source(id="t", name="T", bundesland="SH", system=system, base_url="https://example.org/")


def test_every_supported_system_has_an_adapter():
    for system in ("sessionnet", "allris", "oparl"):
        adapter = get_adapter(_src(system))
        assert isinstance(adapter, SourceAdapter)
        assert adapter.system == system


def test_unknown_system_is_rejected_with_hint():
    with pytest.raises(UnknownSystemError):
        get_adapter(_src("unknown"))


def test_adapter_rejects_mismatching_source():
    with pytest.raises(ValueError):
        ADAPTERS["sessionnet"](_src("allris"))


def test_enabled_sources_have_adapters():
    s = load_settings()
    for src in load_sources(s):
        if src.enabled:
            assert src.system in ADAPTERS
