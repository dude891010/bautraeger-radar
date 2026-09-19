"""Adapter für ALLRIS (CC e-gov), z. B. Hamburger Bezirksversammlungen. Weiterhin ein Stub.

**Status: blocked (Stand 19.09.2026)**, siehe CLAUDE.md "Referenzquelle ALLRIS/Hamburger Umland"
und `config/sources.yaml` (Quelle "wedel"). Die Hamburger Bezirks-ALLRIS-Seiten sperren generische
Crawler pauschal per robots.txt. Die robots.txt-offenen Kandidaten im Hamburger Umland (Wedel, Amt
Siek, Amt Hohe Elbgeest, Amt Pinnau) laufen alle auf "ALLRIS net" Version 4.1.7 (Apache Wicket) -
einer komplett AJAX-/session-basierten Oberfläche ohne einfache, GET-basierte Seiten wie das im
alten Fingerabdruck vermutete `si010.asp`. Ein Adapter dafür bräuchte entweder eine Nachbildung des
Wicket-AJAX-Protokolls oder einen Headless-Browser - Letzteres eine bewusste Architekturentscheidung
GEGEN, um das System leichtgewichtig und ohne Browser-Abhängigkeit wartbar zu halten. Reaktivieren,
falls eine klassische, GET-basierte ALLRIS-Installation im Zielgebiet auftaucht, oder falls die
Architekturentscheidung gegen einen Headless-Browser revidiert wird.
"""

from datetime import date

from radar.sources.base import Dokument, Sitzung, SourceAdapter


class AllrisAdapter(SourceAdapter):
    system = "allris"

    def list_sessions(self, start: date, end: date) -> list[Sitzung]:
        raise NotImplementedError("Schritt 6")

    def list_documents(self, sitzung: Sitzung) -> list[Dokument]:
        raise NotImplementedError("Schritt 6")
