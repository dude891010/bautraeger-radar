"""Zuordnung System-Typ -> Adapter. Neue Systeme werden hier registriert."""

from radar.config import Source
from radar.sources.allris import AllrisAdapter
from radar.sources.base import SourceAdapter
from radar.sources.bv_hh import BvHhAdapter
from radar.sources.hamburg_transparenz import HamburgTransparenzAdapter
from radar.sources.oparl import OParlAdapter
from radar.sources.sessionnet import SessionNetAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    "sessionnet": SessionNetAdapter,
    "allris": AllrisAdapter,
    "oparl": OParlAdapter,
    "hamburg_transparenz": HamburgTransparenzAdapter,
    "bv_hh": BvHhAdapter,
}


class UnknownSystemError(ValueError):
    pass


def get_adapter(source: Source, http=None) -> SourceAdapter:
    try:
        adapter_cls = ADAPTERS[source.system]
    except KeyError:
        raise UnknownSystemError(
            f"Kein Adapter für System '{source.system}' (Quelle '{source.id}'). "
            "Erst Systemtyp klären (docs/QUELLENKATALOG.md)."
        ) from None
    return adapter_cls(source, http)
