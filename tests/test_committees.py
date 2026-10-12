from radar.committees import committee_matches
from radar.config import load_settings

DEFAULTS = load_settings().filters.default_committee_patterns

# Ausschussnamen aus echten Seiten (Norderstedt-Kalender, Hamburg-Bergedorf)
RELEVANT = [
    "Ausschuss für Stadtentwicklung und Verkehr",
    "Stadtentwicklungsausschuss",
    "Fachausschuss für Bauangelegenheiten",
    "Unterausschuss Bau",
    "Planungsausschuss",
    "Bau- und Umweltausschuss",
    "Bauausschuss",
    "Hochbauausschuss",
    "Ausschuss  für\u00a0Stadtplanung",  # doppelte/geschützte Leerzeichen
]
NICHT_RELEVANT = [
    "Hauptausschuss",
    "Umweltausschuss",
    "Ausschuss für Schule und Sport",
    "Kulturausschuss",
    "Sozialausschuss",
    "Jugendhilfeausschuss",
    "Fachausschuss für Verkehr und Inneres",
    "Regionalausschuss",
    "Stadtvertretung",
    "Kinder- und Jugendbeirat",
    "Seniorenbeirat",
]


def test_relevant_committees_match():
    for name in RELEVANT:
        assert committee_matches(name, DEFAULTS), name


def test_irrelevant_committees_do_not_match():
    for name in NICHT_RELEVANT:
        assert not committee_matches(name, DEFAULTS), name


def test_norderstedt_exact_pattern():
    pattern = ["^Ausschuss für Stadtentwicklung und Verkehr$"]
    assert committee_matches("Ausschuss für Stadtentwicklung und Verkehr", pattern)
    assert not committee_matches("Umweltausschuss", pattern)


def test_prefilter_keywords_cover_concrete_building_applications():
    """Kundenwunsch Hummel (10/2026): Vorlagen zu Bauanträgen/Baugenehmigungen dürfen nicht schon
    am kostenlosen Keyword-Vorfilter (Stufe 1) scheitern."""
    from radar.parser import matched_keywords

    keywords = load_settings().filters.prefilter_keywords
    for text in (
        "Bauanträge Neubau Mehrgeschosser",
        "Erteilung der Baugenehmigung",
        "Bauvoranfrage zur Errichtung von 8 WE",
        "Herstellung des gemeindlichen Einvernehmens nach § 36 BauGB",
    ):
        assert matched_keywords(text, keywords), text
