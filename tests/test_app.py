"""Tests für die reine Datenlogik des Dashboards (app.py): Filtern, Diffen von Bearbeitungen,
Export. Die `st.*`-Aufrufe stecken in `main()` und werden hier nicht getestet (dafür bräuchte es
eine laufende Streamlit-Session) - siehe app.py-Docstring."""

import sys
from io import BytesIO

import altair as alt
import pandas as pd
import pdfplumber

from app import (
    HUMMEL_CSS,
    HUMMEL_ORANGE,
    HUMMEL_ORANGE_DARK,
    PRUEF_BADGE,
    PRUEF_HIGHLIGHT_FARBE,
    _pdf_safe,
    _row_highlight,
    _scrape_command,
    add_pruef_badge,
    apply_filters,
    apply_search,
    build_projekte_df,
    chart_projekte_je_status,
    chart_we_je_region,
    compute_kpis,
    export_view,
    find_changed_rows,
    projekte_je_status,
    to_csv_bytes,
    to_excel_bytes,
    to_pdf_bytes,
    we_je_region,
)


def _row(**kwargs):
    defaults = dict(
        id=1,
        quelle_id="norderstedt",
        bundesland="SH",
        ort="Norderstedt",
        wohnform="MIETE",
        verfahrensstand="BAUGENEHMIGUNG",
        status="Neu",
        notizen="",
        projektbezeichnung="Wohnpark Ochsenzoll",
        we_gesamt=18,
    )
    defaults.update(kwargs)
    return defaults


# -- build_projekte_df ----------------------------------------------------------------------------


def test_build_projekte_df_adds_tier_from_mapping():
    df = build_projekte_df([_row()], tier_by_quelle={"norderstedt": "A"})
    assert df.loc[0, "tier"] == "A"


def test_build_projekte_df_unknown_quelle_gets_placeholder_tier():
    df = build_projekte_df([_row(quelle_id="unbekannt")], tier_by_quelle={"norderstedt": "A"})
    assert df.loc[0, "tier"] == "?"


def test_build_projekte_df_empty_rows_returns_empty_frame():
    df = build_projekte_df([], tier_by_quelle={})
    assert df.empty


def test_build_projekte_df_keeps_we_gesamt_as_int_when_some_rows_unknown():
    """CLAUDE.md: fehlende WE-Zahl wird geflaggt statt verworfen ('unknown_units_policy: flag') -
    ein gemischter Datensatz (manche Projekte mit, manche ohne WE-Zahl) ist der Normalfall. Ohne
    das nullable Int64 würde pandas die ganze Spalte zu float64 hochkonvertieren und "18.0" statt
    "18" anzeigen/exportieren, sobald auch nur ein Projekt eine unbekannte WE-Zahl hat."""
    df = build_projekte_df(
        [_row(id=1, we_gesamt=18), _row(id=2, we_gesamt=None)], tier_by_quelle={"norderstedt": "A"}
    )
    assert str(df["we_gesamt"].dtype) == "Int64"
    assert str(df.loc[0, "we_gesamt"]) == "18"  # nicht "18.0"


# -- apply_filters ----------------------------------------------------------------------------------


def _df():
    return build_projekte_df(
        [
            _row(id=1, bundesland="SH", ort="Norderstedt", wohnform="MIETE", status="Neu"),
            _row(id=2, bundesland="HH", ort="Hamburg", wohnform="GEFOERDERT", status="Archiviert"),
        ],
        tier_by_quelle={"norderstedt": "A"},
    )


def test_apply_filters_no_filters_returns_everything():
    result = apply_filters(_df())
    assert len(result) == 2


def test_apply_filters_by_bundesland():
    result = apply_filters(_df(), bundesland=["HH"])
    assert list(result["id"]) == [2]


def test_apply_filters_combines_multiple_filters_with_and():
    result = apply_filters(_df(), bundesland=["SH"], wohnform=["GEFOERDERT"])
    assert result.empty  # SH-Projekt ist MIETE, nicht GEFOERDERT


def test_apply_filters_by_status():
    result = apply_filters(_df(), status=["Archiviert"])
    assert list(result["id"]) == [2]


# -- apply_search (Freitextsuche) --------------------------------------------------------------------


def test_apply_search_empty_query_returns_everything():
    assert len(apply_search(_df(), "")) == 2
    assert len(apply_search(_df(), None)) == 2
    assert len(apply_search(_df(), "   ")) == 2


def test_apply_search_matches_projektbezeichnung_case_insensitive():
    df = build_projekte_df(
        [_row(id=1, projektbezeichnung="Wohnpark Ochsenzoll"), _row(id=2, projektbezeichnung="Quartier Vierlanden")],
        tier_by_quelle={},
    )
    result = apply_search(df, "ochsenzoll")
    assert list(result["id"]) == [1]


def test_apply_search_matches_strasse_and_ort_and_antragsteller():
    df = build_projekte_df(
        [
            _row(id=1, strasse="Holstenkamp", ort="Hamburg-Altona", antragsteller="IBA Altona GmbH"),
            _row(id=2, strasse="Rohrstraße", ort="Bremen-Vegesack", antragsteller="Bremische GmbH"),
        ],
        tier_by_quelle={},
    )
    assert list(apply_search(df, "Holstenkamp")["id"]) == [1]
    assert list(apply_search(df, "vegesack")["id"]) == [2]
    assert list(apply_search(df, "IBA Altona")["id"]) == [1]


def test_apply_search_no_match_returns_empty():
    result = apply_search(_df(), "nicht vorhanden")
    assert result.empty


def test_apply_search_handles_missing_values_without_error():
    """CLAUDE.md: unklare Fälle (fehlende projektbezeichnung) bleiben im Dashboard sichtbar - die
    Suche darf daran nicht mit einer Exception scheitern (pandas macht aus None oft float('nan'),
    siehe apply_search()-Docstring)."""
    df = build_projekte_df([_row(id=1, projektbezeichnung=None, strasse="Möllner Straße")], tier_by_quelle={})
    result = apply_search(df, "Möllner")
    assert list(result["id"]) == [1]
    assert apply_search(df, "gibt es nicht").empty


def test_apply_search_combines_with_apply_filters():
    df = _df()  # id=1 SH/Norderstedt/MIETE, id=2 HH/Hamburg/GEFOERDERT
    gefiltert = apply_filters(apply_search(df, "Ochsenzoll"), bundesland=["SH"])
    assert list(gefiltert["id"]) == [1]


# -- find_changed_rows -----------------------------------------------------------------------------


def test_find_changed_rows_detects_status_change():
    original = pd.DataFrame([{"id": 1, "status": "Neu", "notizen": ""}])
    edited = pd.DataFrame([{"id": 1, "status": "In Prüfung", "notizen": ""}])
    changes = find_changed_rows(original, edited)
    assert changes == [
        {"id": 1, "status": "In Prüfung", "notizen": "", "status_geaendert": True, "alter_status": "Neu"}
    ]


def test_find_changed_rows_detects_notizen_change_without_status_change():
    original = pd.DataFrame([{"id": 1, "status": "Neu", "notizen": ""}])
    edited = pd.DataFrame([{"id": 1, "status": "Neu", "notizen": "Vertrieb informiert"}])
    changes = find_changed_rows(original, edited)
    assert len(changes) == 1
    assert changes[0]["status_geaendert"] is False
    assert changes[0]["notizen"] == "Vertrieb informiert"


def test_find_changed_rows_no_changes_returns_empty_list():
    df = pd.DataFrame([{"id": 1, "status": "Neu", "notizen": ""}])
    assert find_changed_rows(df, df.copy()) == []


def test_find_changed_rows_only_reports_changed_of_multiple_rows():
    original = pd.DataFrame([{"id": 1, "status": "Neu", "notizen": ""}, {"id": 2, "status": "Neu", "notizen": ""}])
    edited = pd.DataFrame([{"id": 1, "status": "Neu", "notizen": ""}, {"id": 2, "status": "Archiviert", "notizen": ""}])
    changes = find_changed_rows(original, edited)
    assert len(changes) == 1
    assert changes[0]["id"] == 2


# -- Export -----------------------------------------------------------------------------------------


def test_export_view_excludes_computed_tier_column():
    df = build_projekte_df([_row()], tier_by_quelle={"norderstedt": "A"})
    view = export_view(df)
    assert "tier" not in view.columns  # nicht in EXPORT_SPALTEN, ist eine Konfig-Ableitung
    assert "projektbezeichnung" in view.columns


def test_export_view_orders_columns_like_export_spalten():
    df = build_projekte_df([_row(status="Neu", notizen="x")], tier_by_quelle={})
    view = export_view(df)
    # we_gesamt steht in EXPORT_SPALTEN vor status, unabhängig von der Spaltenreihenfolge im Row-Dict
    assert list(view.columns).index("we_gesamt") < list(view.columns).index("status")


def test_to_csv_bytes_roundtrips_content():
    df = pd.DataFrame([{"projektbezeichnung": "Wohnpark Ochsenzoll", "ort": "Norderstedt"}])
    csv = to_csv_bytes(df)
    assert isinstance(csv, bytes)
    assert "Wohnpark Ochsenzoll" in csv.decode("utf-8-sig")


def test_export_csv_shows_whole_number_not_float_in_mixed_dataset():
    """End-to-End für den in test_build_projekte_df_keeps_we_gesamt_as_int_when_some_rows_unknown
    beschriebenen Fehler, bis zum tatsächlichen CSV-Text: "18", nicht "18.0"."""
    df = build_projekte_df(
        [_row(id=1, we_gesamt=18), _row(id=2, we_gesamt=None)], tier_by_quelle={"norderstedt": "A"}
    )
    csv_text = to_csv_bytes(export_view(df)).decode("utf-8-sig")
    assert "18\n" in csv_text or ",18," in csv_text
    assert "18.0" not in csv_text


def test_to_excel_bytes_produces_readable_workbook():
    df = pd.DataFrame([{"projektbezeichnung": "Wohnpark Ochsenzoll", "we_gesamt": 18}])
    data = to_excel_bytes(df)
    assert isinstance(data, bytes)
    assert data[:2] == b"PK"  # xlsx ist ein ZIP-Container

    from io import BytesIO

    restored = pd.read_excel(BytesIO(data))
    assert restored.loc[0, "projektbezeichnung"] == "Wohnpark Ochsenzoll"


# -- _pdf_safe / to_pdf_bytes (PDF-Report) -----------------------------------------------------------


def test_pdf_safe_passes_through_normal_text():
    assert _pdf_safe("Norderstedt") == "Norderstedt"


def test_pdf_safe_handles_none_and_nan():
    assert _pdf_safe(None) == ""
    assert _pdf_safe(pd.NA) == ""
    assert _pdf_safe(float("nan")) == ""


def test_pdf_safe_replaces_unencodable_characters_instead_of_raising():
    # fpdf2s Kernschriften kennen kein Latin-1-fremdes Zeichen (z. B. dieses Warn-Emoji) - ein
    # einzelnes solches Zeichen in einem gescrapten Projektnamen darf den PDF-Export nicht mit
    # einer Exception abbrechen (siehe _pdf_safe()-Docstring).
    ergebnis = _pdf_safe(f"{PRUEF_BADGE} Sonderzeichen-Test")
    assert isinstance(ergebnis, str)
    assert "Sonderzeichen-Test" in ergebnis


def test_to_pdf_bytes_produces_valid_pdf_magic_bytes():
    df = build_projekte_df([_row()], tier_by_quelle={"norderstedt": "A"})
    data = to_pdf_bytes(export_view(df))
    assert isinstance(data, bytes)
    assert data[:4] == b"%PDF"


def test_to_pdf_bytes_empty_df_still_produces_valid_pdf():
    data = to_pdf_bytes(pd.DataFrame())
    assert data[:4] == b"%PDF"
    with pdfplumber.open(BytesIO(data)) as doc:
        assert "Keine Projekte" in doc.pages[0].extract_text()


def test_to_pdf_bytes_contains_project_data():
    """Prüft über pdfplumber-Textextraktion nur ASCII-sichere Werte (Ortsname ohne Umlaut,
    Bundesland-Kürzel, WE-Zahl) - fpdf2s Kernschriften rendern Umlaute visuell korrekt (manuell
    im Browser/PDF-Viewer geprüft), aber ohne eingebettete ToUnicode-Zuordnung liefert die
    Textextraktion für Umlaute keine verlässlichen Zeichen zurück, unabhängig vom PDF-Inhalt."""
    df = build_projekte_df(
        [_row(id=1, projektbezeichnung="Testquartier", ort="Norderstedt", bundesland="SH", we_gesamt=42)],
        tier_by_quelle={"norderstedt": "A"},
    )
    data = to_pdf_bytes(export_view(df))
    with pdfplumber.open(BytesIO(data)) as doc:
        text = doc.pages[0].extract_text()
    assert "Testquartier" in text
    assert "Norderstedt" in text
    assert "42" in text


def test_to_pdf_bytes_does_not_crash_on_unencodable_characters():
    df = build_projekte_df(
        [_row(id=1, projektbezeichnung=f"{PRUEF_BADGE} Projekt")], tier_by_quelle={"norderstedt": "A"}
    )
    data = to_pdf_bytes(export_view(df))
    assert data[:4] == b"%PDF"


# -- compute_kpis (KPI-Kacheln) ---------------------------------------------------------------------


def test_compute_kpis_empty_df_returns_zero_projects_and_none_sums():
    kpis = compute_kpis(pd.DataFrame())
    assert kpis == {"projekte": 0, "we_gesamt": None, "we_gefoerdert": None, "offen": 0}


def test_compute_kpis_sums_we_and_counts_offen_status():
    df = build_projekte_df(
        [
            _row(id=1, we_gesamt=18, we_gefoerdert_anzahl=6, status="Neu"),
            _row(id=2, we_gesamt=30, we_gefoerdert_anzahl=10, status="In Prüfung"),
            _row(id=3, we_gesamt=12, we_gefoerdert_anzahl=4, status="Archiviert"),
        ],
        tier_by_quelle={},
    )
    kpis = compute_kpis(df)
    assert kpis == {"projekte": 3, "we_gesamt": 60, "we_gefoerdert": 20, "offen": 2}


def test_compute_kpis_none_when_column_entirely_unknown():
    """CLAUDE.md: nicht hochrechnen/raten - fehlt die WE-Zahl überall, ist die Summe `None`
    (Oberfläche zeigt "–"), nicht fälschlich 0."""
    df = build_projekte_df(
        [_row(id=1, we_gesamt=None, we_gefoerdert_anzahl=None)], tier_by_quelle={}
    )
    kpis = compute_kpis(df)
    assert kpis["we_gesamt"] is None
    assert kpis["we_gefoerdert"] is None


# -- projekte_je_status / we_je_region / Diagramme -----------------------------------------------


def test_projekte_je_status_empty_df_returns_empty_frame():
    result = projekte_je_status(pd.DataFrame())
    assert list(result.columns) == ["status", "anzahl"]
    assert result.empty


def test_projekte_je_status_counts_per_status():
    df = build_projekte_df(
        [_row(id=1, status="Neu"), _row(id=2, status="Neu"), _row(id=3, status="Archiviert")],
        tier_by_quelle={},
    )
    result = projekte_je_status(df)
    zaehlung = dict(zip(result["status"], result["anzahl"], strict=True))
    assert zaehlung == {"Neu": 2, "Archiviert": 1}


def test_projekte_je_status_respects_given_order_and_appends_unknown():
    df = build_projekte_df(
        [_row(id=1, status="Archiviert"), _row(id=2, status="Neu"), _row(id=3, status="Exotisch")],
        tier_by_quelle={},
    )
    result = projekte_je_status(df, status_reihenfolge=["Neu", "In Prüfung", "Archiviert"])
    # bekannte Reihenfolge zuerst (Neu vor Archiviert, wie in status_reihenfolge - nicht
    # alphabetisch), unbekannter Status ("Exotisch") hängt hinten an statt zu verschwinden
    assert list(result["status"]) == ["Neu", "Archiviert", "Exotisch"]


def test_we_je_region_empty_df_returns_empty_frame():
    result = we_je_region(pd.DataFrame())
    assert list(result.columns) == ["bundesland", "we_gesamt"]
    assert result.empty


def test_we_je_region_sums_per_bundesland_ignoring_unknown_we():
    df = build_projekte_df(
        [
            _row(id=1, bundesland="SH", we_gesamt=18),
            _row(id=2, bundesland="SH", we_gesamt=24),
            _row(id=3, bundesland="HH", we_gesamt=None),  # CLAUDE.md: nicht hochrechnen -> 0, nicht NaN
        ],
        tier_by_quelle={},
    )
    result = we_je_region(df)
    werte = dict(zip(result["bundesland"], result["we_gesamt"], strict=True))
    assert werte == {"SH": 42, "HH": 0}


def test_we_je_region_sorted_descending():
    df = build_projekte_df(
        [_row(id=1, bundesland="SH", we_gesamt=10), _row(id=2, bundesland="HH", we_gesamt=50)],
        tier_by_quelle={},
    )
    result = we_je_region(df)
    assert list(result["bundesland"]) == ["HH", "SH"]


def test_chart_projekte_je_status_returns_bar_chart_with_matching_data():
    df = build_projekte_df([_row(id=1, status="Neu"), _row(id=2, status="Archiviert")], tier_by_quelle={})
    chart = chart_projekte_je_status(df, ["Neu", "Archiviert"])
    assert isinstance(chart, alt.Chart)
    assert chart.mark["type"] == "bar"
    assert set(chart.data["status"]) == {"Neu", "Archiviert"}


def test_chart_we_je_region_returns_bar_chart_with_matching_data():
    df = build_projekte_df([_row(id=1, bundesland="SH", we_gesamt=18)], tier_by_quelle={})
    chart = chart_we_je_region(df)
    assert isinstance(chart, alt.Chart)
    assert chart.mark["type"] == "bar"
    assert list(chart.data["bundesland"]) == ["SH"]


# -- add_pruef_badge / _row_highlight (Warn-Badge für unsichere Fälle) -------------------------------


def test_add_pruef_badge_empty_df_returns_empty():
    assert add_pruef_badge(pd.DataFrame()).empty


def test_add_pruef_badge_flags_low_konfidenz():
    df = build_projekte_df([_row(id=1, konfidenz=0.3, wohnform="MIETE")], tier_by_quelle={})
    result = add_pruef_badge(df)
    assert result.loc[0, "pruefen"] == PRUEF_BADGE


def test_add_pruef_badge_flags_wohnform_unklar_even_with_high_konfidenz():
    df = build_projekte_df([_row(id=1, konfidenz=0.95, wohnform="UNKLAR")], tier_by_quelle={})
    result = add_pruef_badge(df)
    assert result.loc[0, "pruefen"] == PRUEF_BADGE


def test_add_pruef_badge_leaves_confident_known_cases_unflagged():
    df = build_projekte_df([_row(id=1, konfidenz=0.9, wohnform="MIETE")], tier_by_quelle={})
    result = add_pruef_badge(df)
    assert result.loc[0, "pruefen"] == ""


def test_add_pruef_badge_mixed_dataset_flags_only_uncertain_rows():
    df = build_projekte_df(
        [
            _row(id=1, konfidenz=0.9, wohnform="MIETE"),
            _row(id=2, konfidenz=0.2, wohnform="MIETE"),
            _row(id=3, konfidenz=0.9, wohnform="UNKLAR"),
        ],
        tier_by_quelle={},
    )
    result = add_pruef_badge(df)
    assert list(result["pruefen"]) == ["", PRUEF_BADGE, PRUEF_BADGE]


def test_row_highlight_marks_flagged_rows_with_warn_color():
    row = pd.Series({"a": 1, "pruefen": PRUEF_BADGE})
    assert _row_highlight(row) == [f"background-color: {PRUEF_HIGHLIGHT_FARBE}"] * len(row)


def test_row_highlight_leaves_unflagged_rows_unstyled():
    row = pd.Series({"a": 1, "pruefen": ""})
    assert _row_highlight(row) == ["", ""]


# -- _scrape_command --------------------------------------------------------------------------------


def test_scrape_command_without_notify_omits_flag():
    """Standardfall (Sidebar-Checkbox unmarkiert): kein --notify, damit ein normaler Testlauf per
    Knopfdruck nicht den Vertrieb anmailt."""
    befehl = _scrape_command(notify=False)
    assert befehl == [sys.executable, "-m", "radar", "scrape"]


def test_scrape_command_with_notify_appends_flag():
    befehl = _scrape_command(notify=True)
    assert befehl == [sys.executable, "-m", "radar", "scrape", "--notify"]


# -- HUMMEL_CSS (Branding) ---------------------------------------------------------------------------


def test_hummel_css_placeholders_are_substituted():
    """Regression: HUMMEL_CSS baut sich aus einem String mit __ORANGE__/__ORANGE_DARK__-
    Platzhaltern zusammen (siehe app.py) - ein Tippfehler im Platzhalternamen ließe sich sonst
    erst optisch im Browser bemerken, nicht durch einen Fehler."""
    assert "__ORANGE__" not in HUMMEL_CSS
    assert "__ORANGE_DARK__" not in HUMMEL_CSS
    assert HUMMEL_ORANGE in HUMMEL_CSS
    assert HUMMEL_ORANGE_DARK in HUMMEL_CSS


def test_hummel_css_enlarges_sidebar_logo():
    """Das Hummel-Logo oben in der Sidebar soll größer als Streamlits eingebaute Stufen sein
    (st.logo(..., size="large") rendert nur 32px hoch, siehe main()) - mit `!important`, weil die
    Höhe sonst aus einer von Streamlit generierten CSS-Klasse kommt, nicht aus einem Inline-Style."""
    assert 'img[data-testid="stSidebarLogo"]' in HUMMEL_CSS
    logo_regel = HUMMEL_CSS.split('img[data-testid="stSidebarLogo"]')[1].split("}")[0]
    assert "height: 56px !important" in logo_regel
    # größer als Streamlits "large"-Stufe (32px)
    hoehe_px = int(logo_regel.split("height:")[1].split("px")[0].strip())
    assert hoehe_px > 32


def test_hummel_css_reduces_sidebar_header_spacing_around_logo():
    """Kleineres Margin um das vergrößerte Logo, damit drumherum nicht mehr Leerraum als vorher
    entsteht (Streamlits Default für size="large" war margin: 4px 0, Header-margin-bottom 16px)."""
    assert 'div[data-testid="stSidebarHeader"]' in HUMMEL_CSS
    header_regel = HUMMEL_CSS.split('div[data-testid="stSidebarHeader"]')[1].split("}")[0]
    margin_bottom_px = int(header_regel.split("margin-bottom:")[1].split("px")[0].strip())
    assert margin_bottom_px < 16
