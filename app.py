"""Streamlit-Dashboard (Schritt 5). Start: `streamlit run app.py`.

Reine Datenlogik (Filtern, Diffen von Bearbeitungen, Export) steht als pure Funktionen ohne
`st.*`-Aufrufe oben im Modul und ist damit ohne laufende Streamlit-Session testbar
(`tests/test_app.py`). Alles mit `st.*` steckt in `main()`, das nur unter `streamlit run`
ausgeführt wird (Streamlit führt das Skript dann mit `__name__ == "__main__"` aus).

Streamlit hat **keine eigene Authentifizierung**: im Container (Schritt 7) nur hinter einem
Reverse Proxy/SSO der Firmen-IT oder mit Basic Auth betreiben, siehe CLAUDE.md.
"""

from __future__ import annotations

import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd

from radar import database
from radar.config import PROJECT_ROOT, Settings, load_settings, load_sources

LOG_TAIL_CHARS = 4000

# Spalten aus `projekte`, die in der Datenbank NULLable INTEGER sind (CLAUDE.md: "flag" statt
# Verwerfen bei fehlender WE-Zahl, also ein Normalfall, kein Rand). pandas' normales `int64`
# kennt kein "fehlt" und weicht bei einem einzigen `None` in der Spalte auf `float64` aus - dann
# steht in Tabelle und CSV-Export "18.0" statt "18". Das nullable `"Int64"` vermeidet das.
NULLABLE_INT_SPALTEN = ["we_gesamt", "we_miete", "we_gefoerdert_anzahl", "seite"]


# -- Reine Datenlogik (testbar ohne Streamlit) -----------------------------------------------------


def build_projekte_df(rows: list, tier_by_quelle: dict[str, str]) -> pd.DataFrame:
    """`sqlite3.Row`-Liste aus `database.list_projekte()` in eine DataFrame umwandeln, ergänzt um
    `tier` (kommt aus der Quellen-Konfiguration, nicht aus der Datenbank, siehe CLAUDE.md:
    "Tiers steuern ... Dashboard-Filter")."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["tier"] = df["quelle_id"].map(tier_by_quelle).fillna("?")
    for spalte in NULLABLE_INT_SPALTEN:
        if spalte in df.columns:
            df[spalte] = df[spalte].astype("Int64")
    return df


def apply_filters(
    df: pd.DataFrame,
    *,
    bundesland: list[str] | None = None,
    ort: list[str] | None = None,
    tier: list[str] | None = None,
    wohnform: list[str] | None = None,
    verfahrensstand: list[str] | None = None,
    status: list[str] | None = None,
) -> pd.DataFrame:
    """Leere/`None`-Filter lassen die jeweilige Spalte unverändert (kein Filter angewendet)."""
    for spalte, werte in (
        ("bundesland", bundesland),
        ("ort", ort),
        ("tier", tier),
        ("wohnform", wohnform),
        ("verfahrensstand", verfahrensstand),
        ("status", status),
    ):
        if werte:
            df = df[df[spalte].isin(werte)]
    return df


def find_changed_rows(original_df: pd.DataFrame, edited_df: pd.DataFrame) -> list[dict]:
    """Vergleicht `status`/`notizen` zwischen Original und einer im `st.data_editor` bearbeiteten
    Kopie (gleicher Index). Liefert nur tatsächlich geänderte Zeilen."""
    changes = []
    for idx in edited_df.index:
        orig = original_df.loc[idx]
        edit = edited_df.loc[idx]
        status_geaendert = orig["status"] != edit["status"]
        notizen_geaendert = orig["notizen"] != edit["notizen"]
        if status_geaendert or notizen_geaendert:
            changes.append(
                {
                    "id": int(edit["id"]),
                    "status": edit["status"],
                    "notizen": edit["notizen"],
                    "status_geaendert": status_geaendert,
                    "alter_status": orig["status"],
                }
            )
    return changes


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")  # BOM, damit Excel unter Windows Umlaute zeigt


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Projekte")
    return buf.getvalue()


EXPORT_SPALTEN = [
    "projektbezeichnung",
    "strasse",
    "hausnummer",
    "ort",
    "kommune",
    "bundesland",
    "we_gesamt",
    "we_miete",
    "we_gefoerdert_anzahl",
    "quote_gefoerdert",
    "ausfuehrungszeitraum",
    "antragsteller",
    "kurzfassung",
    "wohnform",
    "verfahrensstand",
    "konfidenz",
    "status",
    "notizen",
    "quelle_id",
    "aktualisiert_am",
]


def export_view(df: pd.DataFrame) -> pd.DataFrame:
    """Nur die für den Vertrieb relevanten Spalten, in sinnvoller Reihenfolge."""
    vorhandene = [s for s in EXPORT_SPALTEN if s in df.columns]
    return df[vorhandene]


# -- Lauf im Hintergrund (Subprozess statt Thread: isoliert den Anthropic-/DB-Zugriff sauber) ------


def _scrape_command(*, notify: bool) -> list[str]:
    """Befehl für den Hintergrund-Lauf. Über `python -m radar scrape` (radar/cli.py) statt direkt
    `radar.scraper`, damit das optionale `--notify` verfügbar ist - Standard aus, damit ein
    normaler Testlauf per Knopfdruck nicht den Vertrieb anmailt."""
    befehl = [sys.executable, "-m", "radar", "scrape"]
    if notify:
        befehl.append("--notify")
    return befehl


def start_run(settings: Settings, *, notify: bool = False) -> tuple[subprocess.Popen, Path]:
    log_dir = settings.resolve_path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"lauf_{datetime.now():%Y%m%d_%H%M%S}.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            _scrape_command(notify=notify),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
    return process, log_path


def run_is_active(process: subprocess.Popen | None) -> bool:
    return process is not None and process.poll() is None


# -- Streamlit-UI -----------------------------------------------------------------------------------


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Bauträger-Radar", layout="wide")
    st.title("Bauträger-Radar")

    settings = load_settings()
    sources = load_sources(settings)
    tier_by_quelle = {s.id: settings.tier_for(s) for s in sources}

    with st.sidebar:
        st.header("Lauf")
        aktiv = run_is_active(st.session_state.get("lauf_process"))
        sende_mail = st.checkbox(
            "E-Mail-Benachrichtigung senden",
            value=False,
            disabled=aktiv,
            help=(
                "Nur für gezielte Demo-/Produktivläufe aktivieren: löst bei neuen Treffern eine "
                "E-Mail aus (radar/notifier.py, falls SMTP in .env konfiguriert ist). "
                "Unmarkiert bleiben normale Testläufe stumm, auch wenn SMTP konfiguriert ist."
            ),
        )
        if aktiv:
            st.info("Ein Lauf läuft im Hintergrund …")
            if st.button("Aktualisieren"):
                st.rerun()
        elif st.button("Lauf jetzt starten", type="primary"):
            process, log_path = start_run(settings, notify=sende_mail)
            st.session_state["lauf_process"] = process
            st.session_state["lauf_log_path"] = log_path
            st.rerun()

        log_path = st.session_state.get("lauf_log_path")
        if log_path is not None and Path(log_path).exists():
            inhalt = Path(log_path).read_text(encoding="utf-8", errors="replace")
            with st.expander("Log des letzten/aktuellen Laufs", expanded=aktiv):
                st.code(inhalt[-LOG_TAIL_CHARS:] or "(noch keine Ausgabe)")

        st.header("Filter")

    with closing(database.connect(settings)) as conn:
        projekte_rows = database.list_projekte(conn)
        quellen_rows = database.list_quellen(conn)

    df = build_projekte_df(projekte_rows, tier_by_quelle)

    tab_projekte, tab_quellen = st.tabs(["Projekte", "Quellen-Monitoring"])

    with tab_projekte:
        if df.empty:
            st.info("Noch keine Projekte in der Datenbank. Starte oben links einen Lauf.")
        else:
            with st.sidebar:
                f_bundesland = st.multiselect("Bundesland", sorted(df["bundesland"].dropna().unique()))
                f_ort = st.multiselect("Ort", sorted(df["ort"].dropna().unique()))
                f_tier = st.multiselect("Tier", sorted(df["tier"].dropna().unique()))
                f_wohnform = st.multiselect("Wohnform", sorted(df["wohnform"].dropna().unique()))
                f_verfahrensstand = st.multiselect("Verfahrensstand", sorted(df["verfahrensstand"].dropna().unique()))
                f_status = st.multiselect("Status", sorted(df["status"].dropna().unique()))

            gefiltert = apply_filters(
                df,
                bundesland=f_bundesland,
                ort=f_ort,
                tier=f_tier,
                wohnform=f_wohnform,
                verfahrensstand=f_verfahrensstand,
                status=f_status,
            )
            st.caption(f"{len(gefiltert)} von {len(df)} Projekten")

            bearbeitbar = gefiltert[["id", "projektbezeichnung", "ort", "we_gesamt", "status", "notizen"]]
            bearbeitet = st.data_editor(
                bearbeitbar,
                disabled=["id", "projektbezeichnung", "ort", "we_gesamt"],
                column_config={
                    "status": st.column_config.SelectboxColumn("Status", options=settings.dashboard.statuses),
                    "notizen": st.column_config.TextColumn("Notizen"),
                },
                hide_index=True,
                use_container_width=True,
                key="projekte_editor",
            )

            if st.button("Änderungen speichern"):
                changes = find_changed_rows(bearbeitbar, bearbeitet)
                if not changes:
                    st.info("Keine Änderungen.")
                else:
                    with closing(database.connect(settings)) as conn:
                        for change in changes:
                            if change["status_geaendert"]:
                                database.set_projekt_status(conn, change["id"], change["status"])
                            database.set_projekt_notizen(conn, change["id"], change["notizen"])
                    st.success(f"{len(changes)} Projekt(e) aktualisiert.")
                    st.rerun()

            st.subheader("Alle Felder")
            st.dataframe(export_view(gefiltert), hide_index=True, use_container_width=True)

            st.subheader("Export")
            export_df = export_view(gefiltert)
            col_csv, col_excel = st.columns(2)
            with col_csv:
                st.download_button(
                    "CSV herunterladen",
                    data=to_csv_bytes(export_df),
                    file_name="projekte.csv",
                    mime="text/csv",
                )
            with col_excel:
                st.download_button(
                    "Excel herunterladen",
                    data=to_excel_bytes(export_df),
                    file_name="projekte.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

            st.subheader("Vorkommen (Gremien/Sitzungen) eines Projekts")
            if gefiltert.empty:
                # Ohne diese Abfrage würde st.selectbox mit einer leeren Optionsliste aufgerufen,
                # sobald die Sidebar-Filter alles herausfiltern (z. B. eine seltene Status-/
                # Wohnform-Kombination) - unnötig, klarer Hinweis statt eines leeren Widgets.
                st.caption("Kein Projekt entspricht den aktuellen Filtern.")
            else:

                def _projekt_label(i: int) -> str:
                    # projektbezeichnung fehlt bei "unklar"-Projekten oft (CLAUDE.md: unklare
                    # Fälle werden behalten, nicht verworfen) - ohne Fallback stünde hier "None".
                    bezeichnung = df.loc[i, "projektbezeichnung"] or "(ohne Bezeichnung)"
                    return f"{bezeichnung} ({df.loc[i, 'ort']})"

                auswahl = st.selectbox("Projekt", gefiltert.index, format_func=_projekt_label)
                projekt_id = int(df.loc[auswahl, "id"])
                with closing(database.connect(settings)) as conn:
                    vorkommen = database.list_vorlagen_fuer_projekt(conn, projekt_id)
                if vorkommen:
                    vorkommen_df = pd.DataFrame([dict(r) for r in vorkommen])
                    spalten = ["gremium", "sitzungsdatum", "vorlagen_nr", "sitzung_url"]
                    st.dataframe(vorkommen_df[spalten], hide_index=True, use_container_width=True)
                else:
                    st.write("Keine Vorkommen gefunden.")

    with tab_quellen:
        if not quellen_rows:
            st.info("Noch keine Quelle gelaufen.")
        else:
            quellen_df = pd.DataFrame([dict(r) for r in quellen_rows])
            st.dataframe(quellen_df, hide_index=True, use_container_width=True)

    if aktiv:
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
