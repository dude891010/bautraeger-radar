"""Tests für die reine Datenlogik des Dashboards (app.py): Filtern, Diffen von Bearbeitungen,
Export. Die `st.*`-Aufrufe stecken in `main()` und werden hier nicht getestet (dafür bräuchte es
eine laufende Streamlit-Session) - siehe app.py-Docstring."""

import sys

import pandas as pd

from app import (
    _scrape_command,
    apply_filters,
    build_projekte_df,
    export_view,
    find_changed_rows,
    to_csv_bytes,
    to_excel_bytes,
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


# -- _scrape_command --------------------------------------------------------------------------------


def test_scrape_command_without_notify_omits_flag():
    """Standardfall (Sidebar-Checkbox unmarkiert): kein --notify, damit ein normaler Testlauf per
    Knopfdruck nicht den Vertrieb anmailt."""
    befehl = _scrape_command(notify=False)
    assert befehl == [sys.executable, "-m", "radar", "scrape"]


def test_scrape_command_with_notify_appends_flag():
    befehl = _scrape_command(notify=True)
    assert befehl == [sys.executable, "-m", "radar", "scrape", "--notify"]
