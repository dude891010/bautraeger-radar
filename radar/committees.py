"""Erkennen relevanter Ausschüsse (Stadtentwicklung/Planung/Bau) über Regex-Muster.

Die Namen unterscheiden sich je Kommune stark, deshalb Muster statt fester Namen.
Muster pro Quelle stehen in sources.yaml, sonst gelten filters.default_committee_patterns.
"""

import re


def normalize(name: str) -> str:
    """Whitespace vereinheitlichen (auch geschützte Leerzeichen), Ränder trimmen."""
    return re.sub(r"\s+", " ", name.replace("\u00a0", " ")).strip()


def committee_matches(name: str, patterns: list[str]) -> bool:
    text = normalize(name)
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)
