"""Streamlit-Dashboard (Schritt 5). Start: `streamlit run app.py`.

Reine Datenlogik (Filtern, Diffen von Bearbeitungen, Export) steht als pure Funktionen ohne
`st.*`-Aufrufe oben im Modul und ist damit ohne laufende Streamlit-Session testbar
(`tests/test_app.py`). Alles mit `st.*` steckt in `main()`, das nur unter `streamlit run`
ausgeführt wird (Streamlit führt das Skript dann mit `__name__ == "__main__"` aus).

Streamlit hat **keine eigene Authentifizierung** - deshalb ein eigener Passwortdialog
(`radar/auth.py`, `_login_gate`), der vor allem anderen läuft. Ohne `DASHBOARD_PASSWORD` in
`.env` startet das Dashboard nicht (Ausnahme: `DASHBOARD_AUTH=extern` hinter SSO der Firmen-IT).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from io import BytesIO
from pathlib import Path

import altair as alt
import pandas as pd
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from radar import auth, database, demo_data
from radar.config import PROJECT_ROOT, Settings, load_settings, load_sources
from radar.scraper import lauf_sperre_aktiv
from radar.web_recherche import PHASE_LABEL, VERIFIZIERTE_DEMO_URLS, Phase, load_web_config, orte_im_radius

LOG_TAIL_CHARS = 4000
MAX_NOTIZ_LAENGE = 2000

# Spalten aus `projekte`, die in der Datenbank NULLable INTEGER sind (CLAUDE.md: "flag" statt
# Verwerfen bei fehlender WE-Zahl, also ein Normalfall, kein Rand). pandas' normales `int64`
# kennt kein "fehlt" und weicht bei einem einzigen `None` in der Spalte auf `float64` aus - dann
# steht in Tabelle und CSV-Export "18.0" statt "18". Das nullable `"Int64"` vermeidet das.
NULLABLE_INT_SPALTEN = ["we_gesamt", "we_miete", "we_gefoerdert_anzahl", "seite"]

LOGO_PATH = PROJECT_ROOT / "assets" / "hummel_logo.png"

# Hummel-Küchenwerk Corporate Design. Python-Konstanten (statt nur CSS-Variablen), damit auch die
# Altair-Diagramme (siehe unten) dieselben Farben nutzen können, ohne Hex-Werte zu duplizieren.
HUMMEL_ORANGE = "#FF6600"
HUMMEL_ORANGE_DARK = "#E65100"

# Kräftiges Orange als Primärfarbe (Buttons, Tabs, Auswahl-Chips), dunkleres Orange als Hover-/
# Aktiv-Zustand. `[theme]` in .streamlit/config.toml setzt `primaryColor` bereits für die von
# Streamlit selbst eingefärbten Elemente (primäre Buttons, Checkboxen, Tab-Unterstreichung) -
# dieses CSS deckt nur, was das Theme allein nicht erreicht (Hover-Zustand, Sekundär-Buttons,
# Metrik-Kacheln, Auswahl-Chips, Karten-Optik für Tabellen).
HUMMEL_CSS = (
    """
<style>
:root {
    --hummel-orange: __ORANGE__;
    --hummel-orange-dark: __ORANGE_DARK__;
    --hummel-shadow: rgba(43, 35, 29, 0.10);
}

/* Buttons (inkl. Download-Buttons): dezente Orange-Umrandung, kräftiges Orange beim Hover */
.stButton > button, .stDownloadButton > button {
    border-radius: 10px;
    border: 1px solid var(--hummel-orange);
    transition: background-color .15s ease, border-color .15s ease, color .15s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover {
    background-color: var(--hummel-orange-dark);
    border-color: var(--hummel-orange-dark);
    color: #fff;
}
.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"] {
    background-color: var(--hummel-orange);
    border-color: var(--hummel-orange);
}
.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="stBaseButton-primary"]:hover {
    background-color: var(--hummel-orange-dark);
    border-color: var(--hummel-orange-dark);
}

/* Aktiver Tab (Projekte/Quellen-Monitoring) */
button[data-baseweb="tab"][aria-selected="true"] {
    color: var(--hummel-orange-dark) !important;
}
div[data-baseweb="tab-highlight"] {
    background-color: var(--hummel-orange) !important;
}

/* Ausgewählte Chips in Multiselect-Filtern (Bundesland, Ort, Tier, ...) */
span[data-baseweb="tag"] {
    background-color: var(--hummel-orange) !important;
    color: #fff !important;
}

/* Metrik-Kacheln: abgerundete Ecken, dezenter Schatten, klare Innenabstände */
div[data-testid="stMetric"] {
    background: var(--secondary-background-color, #FBEEE3);
    border: 1px solid rgba(255, 102, 0, 0.18);
    border-radius: 16px;
    padding: 1.1rem 1.3rem;
    box-shadow: 0 2px 10px var(--hummel-shadow);
}
div[data-testid="stMetricValue"] {
    color: var(--hummel-orange-dark);
    font-weight: 700;
}

/* Tabellen/Editor als Karte statt scharfkantigem Rechteck */
div[data-testid="stDataFrame"], div[data-testid="stDataEditor"] {
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 10px var(--hummel-shadow);
}

/* Hinweis-/Erfolgsboxen mit Marken-Akzent statt Streamlit-Blau/Grün am linken Rand */
div[data-testid="stAlert"] {
    border-radius: 12px;
    border-left: 4px solid var(--hummel-orange);
}

/* Lead-Badges (Legende im Tab "Kurzfristige Leads") */
.lead-badge {
    display: inline-block;
    padding: 0.15rem 0.65rem;
    margin-right: 0.4rem;
    border-radius: 999px;
    font-size: 0.85rem;
    font-weight: 600;
    border: 1px solid rgba(43, 35, 29, 0.15);
}

/* Hummel-Logo oben in der Sidebar größer als Streamlits größte eingebaute Stufe (st.logo(...,
   size="large") rendert nur 32px hoch) - `stSidebarLogo`/`stSidebarHeader` sind keine offiziell
   dokumentierten testids, sondern am 21.09.2026 live im DOM verifiziert (können sich mit einem
   künftigen Streamlit-Update ändern). Eigenes Bild-Margin und der Sidebar-Header-Abstand darunter
   entsprechend verkleinert, damit rundherum nicht mehr Leerraum als vorher entsteht.
   `!important`, weil die Höhe sonst aus Streamlits eigener, generierter CSS-Klasse kommt. */
img[data-testid="stSidebarLogo"] {
    height: 56px !important;
    width: auto !important;
    margin: 2px 0 !important;
}
div[data-testid="stSidebarHeader"] {
    height: auto !important;
    margin-bottom: 10px !important;
}
</style>
"""
    .replace("__ORANGE__", HUMMEL_ORANGE)
    .replace("__ORANGE_DARK__", HUMMEL_ORANGE_DARK)
)


# -- Reine Datenlogik (testbar ohne Streamlit) -----------------------------------------------------


# Verfahrensstand im Klartext (Spalte "Stand" im Projekte-Tab) - damit sofort sichtbar ist, wie weit ein
# Vorhaben vom Küchenbedarf entfernt ist.
STAND_LABEL = {
    "VORBERATUNG": "Geplant / Vorbereitung",
    "AUFSTELLUNGSBESCHLUSS": "B-Plan / In Planung (Aufstellung)",
    "AUSLEGUNG": "B-Plan / Auslegung",
    "SATZUNGSBESCHLUSS": "B-Plan / beschlossen",
    "BAUANTRAG": "Bauantrag gestellt",
    "BAUGENEHMIGUNG": "Baugenehmigung erteilt",
    "UNKLAR": "unklar",
}
HERKUNFT_ECHT = "⭐ echtes Projekt"
HERKUNFT_DEMO = "Demo (erfunden)"


def add_herkunft_und_stand(df: pd.DataFrame, *, demo_modus: bool) -> pd.DataFrame:
    """Spalten `stand` (Klartext zu `verfahrensstand`) und `herkunft`: geprüfte Echtprojekte
    (`demo_data.ECHTE_QUELLE_ID`) bekommen "⭐ echtes Projekt", im Demo-Modus alle übrigen
    "Demo (erfunden)", sonst bleibt die Herkunft leer (echte Läufe brauchen keine Kennzeichnung)."""
    if df.empty:
        return df
    df = df.copy()
    df["stand"] = df["verfahrensstand"].map(STAND_LABEL).fillna(df["verfahrensstand"])
    echt = df["quelle_id"] == demo_data.ECHTE_QUELLE_ID
    df["herkunft"] = HERKUNFT_DEMO if demo_modus else ""
    df.loc[echt, "herkunft"] = HERKUNFT_ECHT
    return df


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


def compute_kpis(df: pd.DataFrame) -> dict[str, int | None]:
    """Kennzahlen für die KPI-Kacheln oben im Dashboard. `None` (nicht 0!) wenn eine Summe
    mangels Daten nicht gebildet werden kann (CLAUDE.md: nicht raten/hochrechnen) - die
    Oberfläche zeigt dafür "–" statt einer irreführenden Null."""
    if df.empty:
        return {"projekte": 0, "we_gesamt": None, "we_gefoerdert": None, "offen": 0, "leads": 0}

    def _summe(spalte: str) -> int | None:
        werte = df[spalte].dropna() if spalte in df.columns else pd.Series(dtype="Int64")
        return int(werte.sum()) if not werte.empty else None

    offen = int(df["status"].isin(["Neu", "In Prüfung"]).sum()) if "status" in df.columns else 0
    return {
        "projekte": len(df),
        "we_gesamt": _summe("we_gesamt"),
        "we_gefoerdert": _summe("we_gefoerdert_anzahl"),
        "offen": offen,
        "leads": len(kurzfristige_leads(df)),
    }


# -- Diagramme (eigener "Diagramme"-Tab) -------------------------------------------------------


def projekte_je_status(df: pd.DataFrame, status_reihenfolge: list[str] | None = None) -> pd.DataFrame:
    """Anzahl Projekte je Status, für das Status-Diagramm. `status_reihenfolge` (i. d. R.
    `settings.dashboard.statuses`) sortiert die bekannte Pipeline zuerst, damit die Balken immer
    in derselben, sinnvollen Reihenfolge erscheinen statt alphabetisch zu springen; ein im
    Dashboard frei eingetragener, unbekannter Status landet dahinter statt zu verschwinden."""
    if df.empty or "status" not in df.columns:
        return pd.DataFrame(columns=["status", "anzahl"])
    zaehlung = df["status"].value_counts().rename_axis("status").reset_index(name="anzahl")
    if status_reihenfolge:
        bekannt = [s for s in status_reihenfolge if s in zaehlung["status"].values]
        unbekannt = sorted(set(zaehlung["status"]) - set(bekannt))
        reihenfolge = bekannt + unbekannt
        zaehlung["status"] = pd.Categorical(zaehlung["status"], categories=reihenfolge, ordered=True)
        zaehlung = zaehlung.sort_values("status")
    return zaehlung.reset_index(drop=True)


def we_je_region(df: pd.DataFrame) -> pd.DataFrame:
    """Summe der geplanten Wohneinheiten (`we_gesamt`) je Bundesland, für das Regions-Diagramm,
    absteigend sortiert. Projekte ohne WE-Zahl tragen nichts zur Summe bei (CLAUDE.md: nicht
    hochrechnen/raten), die Region selbst bleibt mit ihren übrigen Projekten trotzdem im
    Diagramm sichtbar statt unterzugehen."""
    if df.empty or "bundesland" not in df.columns or "we_gesamt" not in df.columns:
        return pd.DataFrame(columns=["bundesland", "we_gesamt"])
    gruppiert = df.groupby("bundesland")["we_gesamt"].apply(lambda s: int(s.dropna().sum()))
    ergebnis = gruppiert.rename("we_gesamt").reset_index()
    return ergebnis.sort_values("we_gesamt", ascending=False).reset_index(drop=True)


def chart_projekte_je_status(df: pd.DataFrame, status_reihenfolge: list[str] | None = None) -> alt.Chart:
    """Balkendiagramm "Projekte nach Status". Ein Maß (Anzahl) über einer bereits durch die
    x-Achsenbeschriftung eindeutig benannten Kategorie (Status) - Farbe kodiert hier bewusst nur
    zusätzlich zur Position (nicht redundant-notwendig), deshalb ohne eigene Legende."""
    daten = projekte_je_status(df, status_reihenfolge)
    reihenfolge = list(daten["status"])
    return (
        alt.Chart(daten)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=36)
        .encode(
            x=alt.X("status:N", sort=reihenfolge, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("anzahl:Q", title="Projekte", axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("status:N", sort=reihenfolge, scale=alt.Scale(scheme="oranges"), legend=None),
            tooltip=[alt.Tooltip("status:N", title="Status"), alt.Tooltip("anzahl:Q", title="Projekte")],
        )
        .properties(height=320)
    )


def chart_we_je_region(df: pd.DataFrame) -> alt.Chart:
    """Balkendiagramm "Geplante Wohneinheiten nach Region" (Bundesland-Kürzel, wie in den
    übrigen Filtern auch - siehe apply_filters()). Einzelnes Maß je Kategorie, ebenfalls ohne
    Legende (Kategorie steht schon auf der Achse)."""
    daten = we_je_region(df)
    reihenfolge = list(daten["bundesland"])
    return (
        alt.Chart(daten)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=36)
        .encode(
            x=alt.X("bundesland:N", sort=reihenfolge, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("we_gesamt:Q", title="Wohneinheiten"),
            color=alt.Color("bundesland:N", sort=reihenfolge, scale=alt.Scale(scheme="oranges"), legend=None),
            tooltip=[
                alt.Tooltip("bundesland:N", title="Bundesland"),
                alt.Tooltip("we_gesamt:Q", title="Wohneinheiten"),
            ],
        )
        .properties(height=320)
    )


# CLAUDE.md: unklare Fälle (fehlende WE-Zahl/Wohnform) werden nie stillschweigend verworfen,
# sondern geflaggt ("prüfen") - dieselbe Regel gilt für eine LLM-Extraktion mit niedriger
# `konfidenz`, auch wenn Zahl und Wohnform formal gesetzt sind. Schwelle bewusst als einfache
# Konstante statt Konfigurationsoption: eine erste, grobe Grenze, keine fachlich abgestimmte
# Vorgabe vom Vertrieb.
KONFIDENZ_SCHWELLE = 0.6
PRUEF_BADGE = "⚠ prüfen"
PRUEF_HIGHLIGHT_FARBE = "#FFE8CC"  # dezentes Warn-Orange, passend zum Corporate Design


def _leer(wert: object) -> bool:
    return wert is None or (not isinstance(wert, str) and pd.isna(wert))


def pruef_gruende(zeile: pd.Series, *, min_units: int = 6, eigentum_min_we: int = 12) -> list[str]:
    """Warum ein Projekt geprüft werden muss und WAS zu tun ist - je Grund ein Satz mit Auftrag.
    Leere Liste = nichts zu prüfen. Aus den Daten abgeleitet, damit es für jedes künftige Projekt
    gilt (Dashboard: Spalte "Prüfauftrag" und Liste "Was ist zu prüfen?")."""
    gruende = []
    if zeile.get("wohnform", "UNKLAR") == "UNKLAR":
        quote = zeile.get("quote_gefoerdert")
        bekannt = (
            f"Geförderter Anteil bekannt (ca. {quote:.0%}), für den Rest nennt die Quelle nicht, ob Miete oder "
            "Eigentum."
            if not _leer(quote)
            else "Die Quelle sagt nicht, ob Miete, gefördert oder Eigentum."
        )
        gruende.append(
            f"Wohnform unklar: {bekannt} Prüfen: beim Bauträger oder in den Anlagen klären - relevant ab "
            f"{min_units} Miet-/geförderten WE bzw. {eigentum_min_we} Eigentums-WE."
        )
    if all(_leer(zeile.get(s)) for s in ("we_gesamt", "we_miete", "we_gefoerdert_anzahl")):
        gruende.append(
            "Keine Zahl der Wohneinheiten gefunden. Prüfen: Anzahl in den Anlagen (Begründung, städtebaulicher "
            "Vertrag) oder beim Bauträger nachsehen."
        )
    konfidenz = zeile.get("konfidenz", 1.0)
    konfidenz = 0.0 if _leer(konfidenz) else float(konfidenz)
    if konfidenz < KONFIDENZ_SCHWELLE:
        gruende.append(
            f"Die KI war sich unsicher (Konfidenz {konfidenz:.0%}). Prüfen: WE-Zahl, Adresse und Verfahrensstand "
            "mit der Originalquelle abgleichen."
        )
    return gruende


def add_pruef_badge(df: pd.DataFrame, *, min_units: int = 6, eigentum_min_we: int = 12) -> pd.DataFrame:
    """Fügt die Spalten `pruefen` (`PRUEF_BADGE` oder "") und `pruefauftrag` (Grund + Auftrag, siehe
    `pruef_gruende()`) hinzu. Geprüft wird bei unklarer Wohnform, fehlender WE-Zahl (CLAUDE.md: "Fehlt
    die Anzahl oder die Wohnform ... prüfen") oder Konfidenz unter `KONFIDENZ_SCHWELLE`. Damit gehen
    unsichere Treffer nicht zwischen sicheren unter ("Ein Fehlalarm ist billiger als ein verpasstes Projekt")."""
    if df.empty:
        return df
    df = df.copy()
    gruende = df.apply(lambda z: pruef_gruende(z, min_units=min_units, eigentum_min_we=eigentum_min_we), axis=1)
    df["pruefauftrag"] = gruende.map(lambda g: " · ".join(g))
    df["pruefen"] = gruende.map(lambda g: PRUEF_BADGE if g else "")
    return df


def _row_highlight(row: pd.Series) -> list[str]:
    """CSS je Tabellenzeile für die "Alle Felder"-Ansicht (per `Styler.apply(..., axis=1)`):
    unsichere Fälle (siehe `add_pruef_badge()`) bekommen einen dezenten Warn-Hintergrund."""
    stil = f"background-color: {PRUEF_HIGHLIGHT_FARBE}" if row.get("pruefen") == PRUEF_BADGE else ""
    return [stil] * len(row)


# -- Kurzfristige Leads (Kundenfeedback Hummel, 10/2026) -------------------------------------------
# Ein B-Plan-Aufstellungsbeschluss liegt oft Jahre vor dem Küchenbedarf; Bauanträge und erteilte
# Baugenehmigungen sind dagegen konkret und zeitnah. Diese Projekte stehen deshalb in einem
# eigenen, zuerst angezeigten Tab - ohne dass der Vertrieb erst filtern muss.
LEAD_BADGE_GENEHMIGUNG = "🟢 Baugenehmigung"
LEAD_BADGE_BAUANTRAG = "🔵 Bauantrag"
LEAD_BADGE_HINWEIS = "🔎 Hinweis im Text"
LEAD_FARBEN = {
    LEAD_BADGE_GENEHMIGUNG: "#DFF3E4",
    LEAD_BADGE_BAUANTRAG: "#E1ECFA",
    LEAD_BADGE_HINWEIS: "#F4F0E8",
}
# Reihenfolge = Priorität in der Lead-Tabelle (Genehmigung ist am nächsten am Küchenbedarf).
LEAD_PRIORITAET = {LEAD_BADGE_GENEHMIGUNG: 0, LEAD_BADGE_BAUANTRAG: 1, LEAD_BADGE_HINWEIS: 2}

# Das LLM ordnet den Verfahrensstand nicht immer eindeutig zu. Nennt die Extraktion bei
# unklarem/frühem Stand trotzdem eine Baugenehmigung oder einen Bauantrag, ist das ein Hinweis,
# kein gesicherter Lead (deshalb eigenes Badge). Bei B-Plan-Verfahren bewusst NICHT angewendet:
# dort ist "spätere Bauanträge ..." Normalfall und kein kurzfristiges Signal.
LEAD_HINWEIS_STAENDE = {"UNKLAR", "VORBERATUNG"}
LEAD_HINWEIS_BEGRIFFE = ["baugenehmigung", "bauantr", "bauvoranfrage", "vorbescheid", "einvernehmen"]
LEAD_HINWEIS_SPALTEN = ["ausfuehrungszeitraum", "kurzfassung", "evidenz", "projektbezeichnung"]

LEAD_SPALTEN = [
    "lead",
    "projektbezeichnung",
    "strasse",
    "hausnummer",
    "ort",
    "bundesland",
    "we_gesamt",
    "we_miete",
    "we_gefoerdert_anzahl",
    "ausfuehrungszeitraum",
    "antragsteller",
    "kurzfassung",
    "pruefen",
    "pruefauftrag",
    "status",
]


def add_lead_badge(df: pd.DataFrame) -> pd.DataFrame:
    """Ergänzt die Spalte `lead`: `LEAD_BADGE_GENEHMIGUNG`/`LEAD_BADGE_BAUANTRAG` nach dem
    extrahierten `verfahrensstand`, `LEAD_BADGE_HINWEIS`, wenn bei unklarem/frühem Stand die
    Textfelder auf ein konkretes Bauvorhaben hindeuten (siehe `LEAD_HINWEIS_STAENDE`), sonst "".
    `fillna("")` aus demselben Grund wie in `apply_search()` (pandas-NaN in Objekt-Spalten)."""
    if df.empty:
        return df
    df = df.copy()
    stand = df["verfahrensstand"].fillna("UNKLAR") if "verfahrensstand" in df.columns else pd.Series(
        "UNKLAR", index=df.index
    )
    text = pd.Series("", index=df.index)
    for spalte in LEAD_HINWEIS_SPALTEN:
        if spalte in df.columns:
            text = text + " " + df[spalte].fillna("").astype(str).str.lower()
    hinweis = text.apply(lambda t: any(begriff in t for begriff in LEAD_HINWEIS_BEGRIFFE))

    df["lead"] = ""
    df.loc[stand.isin(LEAD_HINWEIS_STAENDE) & hinweis, "lead"] = LEAD_BADGE_HINWEIS
    df.loc[stand == "BAUANTRAG", "lead"] = LEAD_BADGE_BAUANTRAG
    df.loc[stand == "BAUGENEHMIGUNG", "lead"] = LEAD_BADGE_GENEHMIGUNG
    return df


def kurzfristige_leads(df: pd.DataFrame) -> pd.DataFrame:
    """Nur Projekte mit Lead-Badge, sortiert nach Priorität (Genehmigung vor Bauantrag vor
    Hinweis), innerhalb gleicher Priorität nach WE-Zahl absteigend (größere Objekte zuerst,
    unbekannte WE-Zahl zuletzt statt verworfen). Archivierte Projekte bleiben draußen - die hat
    der Vertrieb bewusst abgelegt."""
    if df.empty or "lead" not in df.columns:
        return df.iloc[0:0]
    leads = df[df["lead"] != ""]
    if "status" in leads.columns:
        leads = leads[leads["status"] != "Archiviert"]
    leads = leads.assign(_prio=leads["lead"].map(LEAD_PRIORITAET))
    sortierung = ["_prio"] + (["we_gesamt"] if "we_gesamt" in leads.columns else [])
    leads = leads.sort_values(sortierung, ascending=[True] + [False] * (len(sortierung) - 1), na_position="last")
    return leads.drop(columns="_prio")


def lead_view(df: pd.DataFrame) -> pd.DataFrame:
    """Spaltenauswahl für die Lead-Tabelle - kompakter als `export_view()`, Badge vorne."""
    return df[[s for s in LEAD_SPALTEN if s in df.columns]]


def _lead_highlight(row: pd.Series) -> list[str]:
    """Zeilenfarbe je Lead-Badge (per `Styler.apply(..., axis=1)`), passend zum Badge-Symbol."""
    farbe = LEAD_FARBEN.get(row.get("lead"))
    return [f"background-color: {farbe}" if farbe else ""] * len(row)


# Spalten, die die Freitextsuche durchsucht - alles, worüber der Vertrieb erfahrungsgemäß sucht
# (Projektname, Adresse, Bauherr), keine internen/Herkunftsfelder wie quelle_id oder Vorlagen-Nr.
SUCH_SPALTEN = ["projektbezeichnung", "strasse", "hausnummer", "ort", "kommune", "antragsteller"]


def apply_search(df: pd.DataFrame, suchtext: str | None) -> pd.DataFrame:
    """Freitextsuche über SUCH_SPALTEN, Groß-/Kleinschreibung egal. Leerer/`None`-Suchtext lässt
    `df` unverändert (wie `apply_filters` bei leeren Filtern). `fillna("")` statt eines
    Null-Checks, weil pandas ein fehlendes `None` in einer gemischten Objekt-Spalte beim Einlesen
    zu `float("nan")` macht (siehe `_projekt_label()`) - `str.contains` bräche daran sonst ab."""
    suchtext = (suchtext or "").strip()
    if not suchtext or df.empty:
        return df
    vorhandene = [s for s in SUCH_SPALTEN if s in df.columns]
    treffer = pd.Series(False, index=df.index)
    for spalte in vorhandene:
        treffer = treffer | df[spalte].fillna("").astype(str).str.contains(suchtext, case=False, regex=False)
    return df[treffer]


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


def find_changed_rows(
    original_df: pd.DataFrame, edited_df: pd.DataFrame, erlaubte_status: list[str] | None = None
) -> list[dict]:
    """Vergleicht `status`/`notizen` zwischen Original und einer im `st.data_editor` bearbeiteten
    Kopie (gleicher Index). Liefert nur tatsächlich geänderte Zeilen.

    Die bearbeitete Tabelle kommt vom Browser und ist damit nicht vertrauenswürdig: die
    Sperrung von Spalten im Editor wirkt nur in der Oberfläche. Deshalb stammt die Projekt-`id`
    immer aus dem Original, nur Zeilen aus dem Original werden berücksichtigt, ein Status
    außerhalb von `erlaubte_status` wird verworfen und Notizen werden auf `MAX_NOTIZ_LAENGE`
    gekürzt."""
    changes = []
    for idx in edited_df.index:
        if idx not in original_df.index:
            continue
        orig = original_df.loc[idx]
        edit = edited_df.loc[idx]
        neuer_status = edit["status"]
        if erlaubte_status is not None and neuer_status not in erlaubte_status:
            neuer_status = orig["status"]
        notizen = "" if pd.isna(edit["notizen"]) else str(edit["notizen"])[:MAX_NOTIZ_LAENGE]
        status_geaendert = orig["status"] != neuer_status
        notizen_geaendert = orig["notizen"] != notizen
        if status_geaendert or notizen_geaendert:
            changes.append(
                {
                    "id": int(orig["id"]),
                    "status": neuer_status,
                    "notizen": notizen,
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
    "herkunft",
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
    "stand",
    "lead",
    "konfidenz",
    "pruefen",
    "pruefauftrag",
    "status",
    "notizen",
    "quelle_id",
    "aktualisiert_am",
]


def export_view(df: pd.DataFrame) -> pd.DataFrame:
    """Nur die für den Vertrieb relevanten Spalten, in sinnvoller Reihenfolge."""
    vorhandene = [s for s in EXPORT_SPALTEN if s in df.columns]
    return df[vorhandene]


# Kompakte Auswahl fürs PDF (übersichtlich statt aller EXPORT_SPALTEN - ein PDF ist zum Überfliegen
# gedacht, nicht zum Nachschlagen jedes Feldes, dafür gibt es den CSV-/Excel-Export).
# (Spalte, Spaltenkopf, Breite in mm bei A4 quer)
PDF_SPALTEN: list[tuple[str, str, int]] = [
    ("projektbezeichnung", "Projekt", 55),
    ("strasse", "Adresse", 45),
    ("ort", "Ort", 35),
    ("bundesland", "BL", 12),
    ("we_gesamt", "WE gesamt", 20),
    ("we_gefoerdert_anzahl", "gefördert", 20),
    ("status", "Status", 22),
    ("verfahrensstand", "Verfahrensstand", 45),
]


def _pdf_safe(text: object) -> str:
    """fpdf2s Kernschriften (Helvetica, ohne eingebettete Unicode-Schrift - CLAUDE.md-Prinzip:
    schlank, keine zusätzliche Font-Datei nötig) können nur Latin-1 darstellen. Ein einzelnes
    Sonderzeichen in einem gescrapten Projektnamen (z. B. ein Emoji oder Gedankenstrich) soll den
    ganzen Report-Download nicht mit einer Exception abbrechen - unbekannte Zeichen werden daher
    ersetzt statt den Fehler nach oben durchschlagen zu lassen."""
    if text is None or pd.isna(text):
        return ""
    return str(text).encode("latin-1", errors="replace").decode("latin-1")


class _ReportPDF(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", size=8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, _pdf_safe(f"Seite {self.page_no()}/{{nb}}"), align="C")


def to_pdf_bytes(df: pd.DataFrame, *, titel: str = "Bauträger-Radar - Projektreport") -> bytes:
    """Übersichtlicher PDF-Report der übergebenen (i. d. R. bereits gefilterten) Projekte: Titel,
    Erstellungszeitpunkt und eine kompakte Tabelle (PDF_SPALTEN) im Querformat. Für den Vertrieb
    gedacht, der einen Ausdruck mitnehmen oder per Mail weiterleiten will (CLAUDE.md-Roadmap:
    "Export als CSV/Excel für den Vertrieb" - PDF ergänzt das um eine druckfertige Variante)."""
    pdf = _ReportPDF(orientation="L", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(230, 81, 0)  # Hummel-Orange-Dark
    pdf.cell(0, 10, _pdf_safe(titel), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(90, 90, 90)
    anzahl = len(df)
    pdf.cell(
        0,
        6,
        _pdf_safe(f"Erstellt am {datetime.now():%d.%m.%Y %H:%M} Uhr - {anzahl} Projekt(e)"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.ln(4)

    vorhandene = [(spalte, kopf, breite) for spalte, kopf, breite in PDF_SPALTEN if spalte in df.columns]
    if not vorhandene or df.empty:
        pdf.set_font("Helvetica", size=11)
        pdf.cell(0, 8, "Keine Projekte für den aktuellen Filter.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return bytes(pdf.output())

    pdf.set_font("Helvetica", size=9)
    with pdf.table(
        col_widths=[breite for _, _, breite in vorhandene],
        headings_style=FontFace(emphasis="BOLD", fill_color=(255, 232, 204)),  # PRUEF_HIGHLIGHT_FARBE
        text_align="LEFT",
    ) as table:
        table.row([_pdf_safe(kopf) for _, kopf, _ in vorhandene])
        for _, zeile in df.iterrows():
            table.row([_pdf_safe(zeile.get(spalte)) for spalte, _, _ in vorhandene])

    return bytes(pdf.output())


# -- Web-Recherche (radar/web_recherche.py): eigener Tab mit eigenem Filter ------------------------
# Presse-/Web-Funde stehen bewusst getrennt von den Projekten aus Ratsunterlagen: sie sind weder
# per LLM ausgewertet noch an eine Vorlage gebunden, und mehrere Artikel können dasselbe Vorhaben
# beschreiben. Der Vertrieb soll das auf einen Blick unterscheiden können.
WEB_PHASE_BADGE = {
    Phase.BAUGENEHMIGUNG.value: f"🟢 {PHASE_LABEL[Phase.BAUGENEHMIGUNG]}",
    Phase.BAUBEGINN.value: f"🏗️ {PHASE_LABEL[Phase.BAUBEGINN]}",
}
WEB_WOHNFORM_LABEL = {
    "MIETE": "Miete/gefördert",
    "GEMISCHT": "Gemischt",
    "EIGENTUM": "Eigentum",
    "UNKLAR": "unklar",
}
# Im Demo-Modus: echte, geprüfte Projekte von den frei erfundenen Beispielen unterscheidbar machen.
WEB_HINWEIS_VERIFIZIERT = "⭐ echtes Projekt"
WEB_HINWEIS_DEMO = "Demo (erfunden)"
DEMO_URL_PRAEFIX = "https://example.org/demo/"

WEB_EXPORT_SPALTEN = [
    "hinweis",
    "phase_label",
    "wohnform_label",
    "ort",
    "bundesland",
    "entfernung_km",
    "titel",
    "we_anzahl",
    "evidenz",
    "auszug",
    "url",
    "domain",
    "veroeffentlicht",
    "status",
    "notizen",
    "aktualisiert_am",
]


def build_web_funde_df(rows: list) -> pd.DataFrame:
    """`database.list_web_funde()` als DataFrame, ergänzt um eine lesbare `phase_label`-Spalte."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["phase_label"] = df["phase"].map(WEB_PHASE_BADGE).fillna(df["phase"])
    df["wohnform_label"] = df["wohnform"].map(WEB_WOHNFORM_LABEL).fillna(df["wohnform"])
    df["we_anzahl"] = df["we_anzahl"].astype("Int64")
    df["hinweis"] = df["url"].map(
        lambda u: WEB_HINWEIS_VERIFIZIERT
        if u in VERIFIZIERTE_DEMO_URLS
        else WEB_HINWEIS_DEMO
        if str(u).startswith(DEMO_URL_PRAEFIX)
        else ""
    )
    return df


def apply_web_filter(
    df: pd.DataFrame,
    *,
    phasen: list[str] | None = None,
    max_km: float | None = None,
    orte: list[str] | None = None,
    status: list[str] | None = None,
    nur_mit_we: bool = False,
    wohnformen: list[str] | None = None,
) -> pd.DataFrame:
    """Filter des Web-Recherche-Tabs. Wie `apply_filters()`: leer/`None` = kein Filter.
    `phasen` sind die Rohwerte (`BAUGENEHMIGUNG`/`BAUBEGINN`), `max_km` der Umkreis um das Zentrum."""
    if df.empty:
        return df
    if phasen:
        df = df[df["phase"].isin(phasen)]
    if max_km is not None:
        df = df[df["entfernung_km"] <= max_km]
    if orte:
        df = df[df["ort"].isin(orte)]
    if status:
        df = df[df["status"].isin(status)]
    if nur_mit_we:
        df = df[df["we_anzahl"].notna()]
    if wohnformen:
        df = df[df["wohnform"].isin(wohnformen)]
    return df


def web_export_view(df: pd.DataFrame) -> pd.DataFrame:
    return df[[s for s in WEB_EXPORT_SPALTEN if s in df.columns]]


# -- Lauf im Hintergrund (Subprozess statt Thread: isoliert den Anthropic-/DB-Zugriff sauber) ------


def _scrape_command(*, notify: bool) -> list[str]:
    """Befehl für den Hintergrund-Lauf. Über `python -m radar scrape` (radar/cli.py) statt direkt
    `radar.scraper`, damit das optionale `--notify` verfügbar ist - Standard aus, damit ein
    normaler Testlauf per Knopfdruck nicht den Vertrieb anmailt."""
    befehl = [sys.executable, "-m", "radar", "scrape"]
    if notify:
        befehl.append("--notify")
    return befehl


def _web_recherche_command(*, demo: bool = False) -> list[str]:
    befehl = [sys.executable, "-m", "radar", "web-recherche"]
    if demo:
        befehl.append("--demo")
    return befehl


def start_run(
    settings: Settings, *, notify: bool = False, befehl: list[str] | None = None, log_praefix: str = "lauf"
) -> tuple[subprocess.Popen, Path]:
    """Startet `befehl` (Standard: der vollständige Lauf) als Subprozess mit eigener Logdatei."""
    log_dir = settings.resolve_path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{log_praefix}_{datetime.now():%Y%m%d_%H%M%S}.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            befehl or _scrape_command(notify=notify),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
    return process, log_path


def run_is_active(process: subprocess.Popen | None) -> bool:
    return process is not None and process.poll() is None


# -- Streamlit-UI -----------------------------------------------------------------------------------


def _login_gate(st) -> None:
    """Passwortdialog vor allen Daten und Aktionen (siehe radar/auth.py). Ruft `st.stop()`, bis
    die Sitzung angemeldet ist - nichts unterhalb wird für Unangemeldete ausgeführt."""
    if auth.auth_extern() or st.session_state.get("angemeldet"):
        if not auth.auth_extern() and st.sidebar.button("Abmelden"):
            st.session_state.clear()
            st.rerun()
        return

    try:
        erwartet = auth.dashboard_passwort()
    except auth.AuthKonfigurationsFehler as exc:
        st.error(str(exc))
        st.stop()

    rest = auth.LOGIN_SPERRE.restsperre_sekunden()
    if rest:
        st.error(f"Zu viele Fehlversuche. Anmeldung in {rest // 60 + 1} Minute(n) wieder möglich.")
        st.stop()

    with st.form("login"):
        eingabe = st.text_input("Passwort", type="password", max_chars=256)
        abgeschickt = st.form_submit_button("Anmelden", type="primary")
    if abgeschickt:
        if auth.passwort_korrekt(eingabe, erwartet):
            st.session_state["angemeldet"] = True
            st.rerun()
        auth.LOGIN_SPERRE.fehlversuch()
        time.sleep(1)  # bremst automatisiertes Durchprobieren zusätzlich
        st.error("Passwort falsch.")
    st.stop()


def _web_recherche_tab(
    st, settings: Settings, active_settings: Settings, web_df: pd.DataFrame, *, demo_modus: bool
) -> bool:
    """Inhalt des Tabs "Web-Recherche". Liefert, ob gerade eine Recherche im Hintergrund läuft
    (dann aktualisiert sich die Seite automatisch, wie beim Hauptlauf)."""
    try:
        web_config = load_web_config(settings)
    except (OSError, ValueError) as exc:
        st.error(f"config/web_recherche.yaml fehlerhaft: {exc}")
        return False

    st.subheader(f"Phase 3 & 4 im Umkreis von {web_config.radius_km:.0f} km um {web_config.zentrum_name}")
    st.caption(
        "Baugenehmigungen und Baubeginne aus Lokalpresse und Pressemitteilungen, gefunden per "
        f"Such-API über {len(orte_im_radius(web_config))} Orte (config/web_recherche.yaml). "
        "Eingeordnet per Stichwort, ohne KI - WE-Zahlen nur, wenn sie wörtlich im Text stehen. "
        "Mehrere Artikel können dasselbe Vorhaben beschreiben."
    )
    if demo_modus:
        st.info(
            "Demo-Modus: frei erfundene Beispielfunde (\"Demo (erfunden)\") - mit Ausnahme der mit "
            f"\"{WEB_HINWEIS_VERIFIZIERT}\" markierten Projekte: die sind real und an der verlinkten Quelle geprüft."
        )

    web_aktiv = run_is_active(st.session_state.get("web_process"))
    col_start, col_log = st.columns([1, 3])
    with col_start:
        live = settings.betrieb.live_api_aufrufe
        if web_aktiv:
            st.info("Web-Recherche läuft …")
        elif not live:
            # Kein Guthaben/Plan: der Knopf startet nur die Demo (0 Credits, schreibt in die Demo-Datenbank).
            st.caption("Live-Betrieb aus (config/settings.yaml: betrieb.live_api_aufrufe) - keine Tavily-Anfragen.")
            if st.button(
                "Demo-Recherche starten (0 Credits)",
                help="Beispieldaten in die Demo-Datenbank - sichtbar mit 'Demo-Daten anzeigen'.",
            ):
                process, log_path = start_run(settings, befehl=_web_recherche_command(demo=True), log_praefix="web")
                st.session_state["web_process"] = process
                st.session_state["web_log_path"] = log_path
                st.rerun()
        elif not os.environ.get("TAVILY_API_KEY", "").strip():
            st.warning("TAVILY_API_KEY fehlt in .env - Recherche nicht möglich.")
        elif st.button("Web-Recherche jetzt starten", help="Läuft immer gegen die echte Datenbank."):
            process, log_path = start_run(settings, befehl=_web_recherche_command(), log_praefix="web")
            st.session_state["web_process"] = process
            st.session_state["web_log_path"] = log_path
            st.rerun()
    with col_log:
        log_path = st.session_state.get("web_log_path")
        if log_path is not None and Path(log_path).exists():
            inhalt = Path(log_path).read_text(encoding="utf-8", errors="replace")
            with st.expander("Log der Web-Recherche", expanded=web_aktiv):
                st.code(inhalt[-LOG_TAIL_CHARS:] or "(noch keine Ausgabe)")

    if web_df.empty:
        st.info("Noch keine Web-Funde. Starte oben eine Web-Recherche.")
        return web_aktiv

    f1, f2, f3, f4 = st.columns([2, 2, 2, 1], gap="medium")
    with f1:
        f_phasen = st.multiselect(
            "Phase",
            [p.value for p in Phase],
            default=[p.value for p in Phase],
            format_func=lambda p: WEB_PHASE_BADGE.get(p, p),
            key="web_phase",
        )
    with f2:
        radius = int(web_config.radius_km)
        f_km = st.slider("Umkreis (km)", min_value=5, max_value=radius, value=radius, step=5, key="web_km")
    with f3:
        f_orte = st.multiselect("Ort", sorted(web_df["ort"].dropna().unique()), key="web_ort")
        f_wohnform = st.multiselect(
            "Wohnform",
            list(WEB_WOHNFORM_LABEL),
            format_func=lambda w: WEB_WOHNFORM_LABEL.get(w, w),
            key="web_wohnform",
            help=f"Reine Eigentumsprojekte erscheinen ab {web_config.eigentum_min_we} WE (Großprojekte sind "
            "B2B-relevant); kleinere werden bei der Recherche verworfen.",
        )
        f_status = st.multiselect("Status", sorted(web_df["status"].dropna().unique()), key="web_status")
    with f4:
        f_nur_we = st.checkbox("nur mit WE-Zahl", key="web_nur_we")

    gefiltert = apply_web_filter(
        web_df,
        phasen=f_phasen,
        max_km=f_km,
        orte=f_orte,
        status=f_status,
        nur_mit_we=f_nur_we,
        wohnformen=f_wohnform,
    )
    st.caption(f"{len(gefiltert)} von {len(web_df)} Funden")

    spalten = ["id", "phase_label", "wohnform_label", "ort", "entfernung_km", "titel", "we_anzahl", "url"]
    if demo_modus:
        spalten.insert(1, "hinweis")
    bearbeitbar = gefiltert[spalten + ["status", "notizen"]]
    bearbeitet = st.data_editor(
        bearbeitbar,
        disabled=spalten,
        column_config={
            "hinweis": st.column_config.TextColumn(
                "Herkunft", help="Echtes, geprüftes Projekt oder erfundenes Beispiel"
            ),
            "phase_label": st.column_config.TextColumn("Phase", width="medium"),
            "wohnform_label": st.column_config.TextColumn("Wohnform"),
            "ort": "Ort",
            "entfernung_km": st.column_config.NumberColumn("km", format="%.0f"),
            "titel": st.column_config.TextColumn("Titel", width="large"),
            "we_anzahl": st.column_config.NumberColumn("WE (laut Text)"),
            "url": st.column_config.LinkColumn("Quelle", display_text="öffnen"),
            "status": st.column_config.SelectboxColumn("Status", options=settings.dashboard.statuses),
            "notizen": st.column_config.TextColumn("Notizen", max_chars=MAX_NOTIZ_LAENGE),
        },
        hide_index=True,
        use_container_width=True,
        key="web_editor",
    )
    col_speichern, col_export = st.columns([1, 3])
    with col_speichern:
        if st.button("Änderungen speichern", key="web_speichern"):
            changes = find_changed_rows(bearbeitbar, bearbeitet, settings.dashboard.statuses)
            if not changes:
                st.info("Keine Änderungen.")
            else:
                with closing(database.connect(active_settings)) as conn:
                    for change in changes:
                        if change["status_geaendert"]:
                            database.set_web_fund_status(conn, change["id"], change["status"])
                        database.set_web_fund_notizen(conn, change["id"], change["notizen"])
                st.success(f"{len(changes)} Fund(e) aktualisiert.")
                st.rerun()
    with col_export:
        st.download_button(
            "Web-Funde als Excel herunterladen",
            data=to_excel_bytes(web_export_view(gefiltert)),
            file_name="web_recherche.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with st.expander("Details: Textauszug und Beleg der WE-Zahl"):
        st.dataframe(
            gefiltert[["titel", "evidenz", "auszug", "domain", "veroeffentlicht", "suchanfrage"]],
            column_config={
                "titel": "Titel",
                "evidenz": "Beleg (Zitat)",
                "auszug": "Auszug",
                "domain": "Website",
                "veroeffentlicht": "veröffentlicht",
                "suchanfrage": "Suchanfrage",
            },
            hide_index=True,
            use_container_width=True,
        )
    return web_aktiv


def main() -> None:
    import streamlit as st

    st.set_page_config(
        page_title="Bauträger-Radar",
        page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "🏗️",
        layout="wide",
    )
    st.markdown(HUMMEL_CSS, unsafe_allow_html=True)
    if LOGO_PATH.exists():
        st.logo(str(LOGO_PATH), size="large")
    st.title("Bauträger-Radar")
    st.caption("Hummel Küchenwerk · Objektgeschäft-Radar für Neubauprojekte")

    settings = load_settings()  # lädt auch .env - muss VOR dem Login passieren (DASHBOARD_PASSWORD)
    _login_gate(st)  # stoppt das Skript hier, solange niemand angemeldet ist

    sources = load_sources(settings)
    tier_by_quelle = {s.id: settings.tier_for(s) for s in sources}
    tier_by_quelle.update(demo_data.ECHTE_TIER_BY_QUELLE)

    with st.sidebar:
        st.header("Demo-Modus")
        demo_modus = st.checkbox(
            "Demo-Daten anzeigen",
            value=False,
            help=(
                "Zeigt 9 synthetische Beispielprojekte aus allen Zielregionen statt der echten "
                "Datenbank - für Präsentationen oder solange noch kein echter Lauf/API-Guthaben "
                "vorhanden ist. Schreibt/liest ausschließlich data/demo/demo.db, nie "
                "data/radar.db."
            ),
        )
        if demo_modus:
            st.caption(
                "Demo-Modus aktiv. 'Lauf jetzt starten' unten läuft trotzdem immer gegen die "
                "echte Datenbank."
            )
            if st.button(
                "Demo-Daten zurücksetzen",
                help=(
                    "Befüllt die Demo-Datenbank neu über dieselbe Seed-Logik wie beim ersten "
                    "Aktivieren - macht im Demo-Modus bearbeitete Status/Notizen rückgängig."
                ),
            ):
                demo_data.reset_demo(settings)
                st.success("Demo-Daten zurückgesetzt.")
                st.rerun()

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
        # Lauf aus einer anderen Quelle (Scheduler, anderer Browser-Tab): radar.scraper.lauf_sperre
        # würde einen zweiten Lauf ohnehin abweisen (sonst doppeltes LLM-Budget) - hier nur sichtbar machen.
        fremder_lauf = not aktiv and lauf_sperre_aktiv(settings)
        with closing(database.connect(settings)) as conn:
            heute_verbraucht = database.llm_anfragen_am(conn, datetime.now().date().isoformat())
        st.caption(f"LLM-Anfragen heute: {heute_verbraucht} von max. {settings.llm.max_calls_per_day}")
        if not settings.betrieb.live_api_aufrufe:
            st.caption(
                "Live-Betrieb aus: ein Lauf lädt die Ratsunterlagen, die KI-Analyse (Anthropic) wartet, "
                "bis betrieb.live_api_aufrufe in config/settings.yaml eingeschaltet ist."
            )
        if aktiv:
            st.info("Ein Lauf läuft im Hintergrund …")
            if st.button("Aktualisieren"):
                st.rerun()
        elif fremder_lauf:
            st.info("Ein anderer Lauf (z. B. der geplante Wochenlauf) ist gerade aktiv.")
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

    # Demo-Modus liest/schreibt eine eigene DB-Datei (data/demo/demo.db, siehe radar/demo_data.py)
    # - berührt nie die echte data/radar.db. Tier-Zuordnung ergänzt um die Demo-Quellen, die es in
    # config/sources.yaml bewusst nicht gibt (keine echten Ratsinformationssysteme).
    if demo_modus:
        active_settings = demo_data.ensure_demo_seeded(settings)
        tier_by_quelle = {**tier_by_quelle, **demo_data.DEMO_TIER_BY_QUELLE}
    else:
        active_settings = settings

    with closing(database.connect(active_settings)) as conn:
        projekte_rows = database.list_projekte(conn)
        quellen_rows = database.list_quellen(conn)
        web_df = build_web_funde_df(database.list_web_funde(conn))

    df = build_projekte_df(projekte_rows, tier_by_quelle)
    pruef_df = add_pruef_badge(
        df, min_units=settings.filters.min_units, eigentum_min_we=settings.filters.eigentum_min_we
    )
    df = add_herkunft_und_stand(add_lead_badge(pruef_df), demo_modus=demo_modus)

    kpis = compute_kpis(df)
    if not df.empty:
        kpi_cols = st.columns(5, gap="medium")
        kpi_cols[0].metric("Projekte", kpis["projekte"])
        kpi_cols[1].metric(
            "Wohneinheiten gesamt", kpis["we_gesamt"] if kpis["we_gesamt"] is not None else "–"
        )
        kpi_cols[2].metric(
            "davon gefördert", kpis["we_gefoerdert"] if kpis["we_gefoerdert"] is not None else "–"
        )
        kpi_cols[3].metric("Offen (Neu / In Prüfung)", kpis["offen"])
        kpi_cols[4].metric(
            "Kurzfristige Leads",
            kpis["leads"],
            help="Baugenehmigungen und Bauanträge - siehe Tab 'Kurzfristige Leads'.",
        )

    # "Kurzfristige Leads" bewusst als ERSTER Tab (Streamlit öffnet immer den ersten): der
    # Vertrieb sieht konkrete Bauvorhaben sofort, ohne zu filtern (Kundenfeedback Hummel, 10/2026).
    tab_leads, tab_web, tab_projekte, tab_diagramme, tab_quellen = st.tabs(
        [
            f"🏗️ Kurzfristige Leads ({kpis['leads']})",
            f"🌐 Web-Recherche ({len(web_df)})",
            "Projekte",
            "Diagramme",
            "Quellen-Monitoring",
        ]
    )

    with tab_leads:
        if demo_modus:
            st.info("Demo-Modus: Es werden synthetische Beispielprojekte angezeigt, keine echten Daten.")
        st.subheader("Konkrete Baugenehmigungen & Bauanträge")
        st.markdown(
            "".join(
                f'<span class="lead-badge" style="background:{LEAD_FARBEN[b]}">{b}</span>'
                for b in (LEAD_BADGE_GENEHMIGUNG, LEAD_BADGE_BAUANTRAG, LEAD_BADGE_HINWEIS)
            ),
            unsafe_allow_html=True,
        )
        st.caption(
            "Projekte mit erteilter Baugenehmigung oder vorliegendem Bauantrag - zeitlich am "
            "nächsten am Küchenbedarf. 'Hinweis im Text': Verfahrensstand unklar, die Unterlagen "
            "erwähnen aber Baugenehmigung/Bauantrag - bitte prüfen. Zeigt alle Projekte "
            "unabhängig von den Filtern links (ohne archivierte)."
        )
        leads = kurzfristige_leads(df)
        if leads.empty:
            st.info(
                "Aktuell keine Baugenehmigungen oder Bauanträge erkannt. Bebauungsplan-Verfahren "
                "stehen weiterhin im Tab 'Projekte'."
            )
        else:
            st.dataframe(
                lead_view(leads).style.apply(_lead_highlight, axis=1),
                column_config={
                    "lead": st.column_config.TextColumn("Stand", width="medium"),
                    "projektbezeichnung": "Projekt",
                    "strasse": "Straße",
                    "hausnummer": "Nr.",
                    "ort": "Ort",
                    "bundesland": "BL",
                    "we_gesamt": "WE gesamt",
                    "we_miete": "Miet-WE",
                    "we_gefoerdert_anzahl": "geförderte WE",
                    "ausfuehrungszeitraum": "Baubeginn/Zeitraum",
                    "antragsteller": "Bauherr/Antragsteller",
                    "kurzfassung": "Kurzfassung",
                    "pruefen": st.column_config.TextColumn("⚠"),
                    "status": "Status",
                },
                hide_index=True,
                use_container_width=True,
            )
            st.download_button(
                "Leads als Excel herunterladen",
                data=to_excel_bytes(export_view(leads)),
                file_name="kurzfristige_leads.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    with tab_web:
        web_aktiv = _web_recherche_tab(st, settings, active_settings, web_df, demo_modus=demo_modus)

    with tab_diagramme:
        if df.empty:
            st.info("Noch keine Projekte in der Datenbank. Starte oben links einen Lauf.")
        else:
            col_status, col_region = st.columns(2, gap="medium")
            with col_status:
                st.subheader("Projekte nach Status")
                st.altair_chart(
                    chart_projekte_je_status(df, settings.dashboard.statuses), use_container_width=True
                )
            with col_region:
                st.subheader("Geplante Wohneinheiten nach Region")
                st.altair_chart(chart_we_je_region(df), use_container_width=True)

    with tab_projekte:
        if demo_modus:
            st.info("Demo-Modus: Es werden synthetische Beispielprojekte angezeigt, keine echten Daten.")
        if df.empty:
            st.info("Noch keine Projekte in der Datenbank. Starte oben links einen Lauf.")
        else:
            with st.sidebar:
                suche = st.text_input(
                    "Suche",
                    placeholder="Projekt, Straße, Ort, Bauherr ...",
                    help="Durchsucht Projektbezeichnung, Adresse, Kommune und Antragsteller.",
                )
                f_bundesland = st.multiselect("Bundesland", sorted(df["bundesland"].dropna().unique()))
                f_ort = st.multiselect("Ort", sorted(df["ort"].dropna().unique()))
                f_tier = st.multiselect("Tier", sorted(df["tier"].dropna().unique()))
                f_wohnform = st.multiselect("Wohnform", sorted(df["wohnform"].dropna().unique()))
                f_verfahrensstand = st.multiselect("Verfahrensstand", sorted(df["verfahrensstand"].dropna().unique()))
                f_status = st.multiselect("Status", sorted(df["status"].dropna().unique()))

            gefiltert = apply_filters(
                apply_search(df, suche),
                bundesland=f_bundesland,
                ort=f_ort,
                tier=f_tier,
                wohnform=f_wohnform,
                verfahrensstand=f_verfahrensstand,
                status=f_status,
            )
            st.caption(f"{len(gefiltert)} von {len(df)} Projekten")
            unsichere_anzahl = int((gefiltert["pruefen"] == PRUEF_BADGE).sum())
            if unsichere_anzahl:
                st.warning(
                    f"{PRUEF_BADGE}: {unsichere_anzahl} Projekt(e) mit unklarer Wohnform, fehlender WE-Zahl oder "
                    "geringer Konfidenz - Grund und Auftrag stehen in der Spalte 'Prüfauftrag' und hier darunter."
                )
                with st.expander(f"Was ist zu prüfen? ({unsichere_anzahl})"):
                    for _, zeile in gefiltert[gefiltert["pruefen"] == PRUEF_BADGE].iterrows():
                        name = zeile["projektbezeichnung"]
                        name = "(ohne Bezeichnung)" if _leer(name) or not name else name
                        st.markdown(f"**{name}** ({zeile['ort']})")
                        for grund in zeile["pruefauftrag"].split(" · "):
                            st.markdown(f"- {grund}")

            nur_lesen = [
                "id", "herkunft", "lead", "pruefen", "pruefauftrag", "projektbezeichnung", "stand", "ort", "we_gesamt"
            ]
            if not (gefiltert["herkunft"] != "").any():
                nur_lesen.remove("herkunft")  # echter Betrieb: keine leere Spalte zeigen
            bearbeitbar = gefiltert[nur_lesen + ["status", "notizen"]]
            bearbeitet = st.data_editor(
                bearbeitbar,
                disabled=nur_lesen,
                column_config={
                    "herkunft": st.column_config.TextColumn(
                        "Herkunft", help="Echtes, an der Quelle geprüftes Projekt oder erfundenes Beispiel"
                    ),
                    "stand": st.column_config.TextColumn(
                        "Stand", help="Verfahrensstand im Klartext - wie weit ist das Vorhaben vom Küchenbedarf?"
                    ),
                    "lead": st.column_config.TextColumn(
                        "Lead", help="Baugenehmigung/Bauantrag - siehe Tab 'Kurzfristige Leads'."
                    ),
                    "pruefen": st.column_config.TextColumn(
                        "⚠",
                        help=(
                            "**Bitte prüfen**, wenn die Wohnform unklar ist, die WE-Zahl fehlt oder die KI unsicher "
                            "war. Was genau zu tun ist, steht je Projekt in der Spalte **Prüfauftrag** und "
                            "vollständig unter **Was ist zu prüfen?** oberhalb der Tabelle."
                        ),
                    ),
                    "pruefauftrag": st.column_config.TextColumn(
                        "Prüfauftrag",
                        width="large",
                        help="Warum geprüft werden muss und was zu tun ist. Ganzer Text: 'Was ist zu prüfen?' oben.",
                    ),
                    "status": st.column_config.SelectboxColumn("Status", options=settings.dashboard.statuses),
                    "notizen": st.column_config.TextColumn("Notizen", max_chars=MAX_NOTIZ_LAENGE),
                },
                hide_index=True,
                use_container_width=True,
                key="projekte_editor",
            )

            if st.button("Änderungen speichern"):
                changes = find_changed_rows(bearbeitbar, bearbeitet, settings.dashboard.statuses)
                if not changes:
                    st.info("Keine Änderungen.")
                else:
                    with closing(database.connect(active_settings)) as conn:
                        for change in changes:
                            if change["status_geaendert"]:
                                database.set_projekt_status(conn, change["id"], change["status"])
                            database.set_projekt_notizen(conn, change["id"], change["notizen"])
                    st.success(f"{len(changes)} Projekt(e) aktualisiert.")
                    st.rerun()

            st.subheader("Alle Felder")
            alle_felder_df = export_view(gefiltert)
            st.dataframe(
                alle_felder_df.style.apply(_row_highlight, axis=1), hide_index=True, use_container_width=True
            )

            st.subheader("Export")
            export_df = export_view(gefiltert)
            col_csv, col_excel, col_pdf = st.columns(3)
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
            with col_pdf:
                st.download_button(
                    "PDF-Report herunterladen",
                    data=to_pdf_bytes(gefiltert),
                    file_name="projektreport.pdf",
                    mime="application/pdf",
                    help="Übersichtlicher, druckfertiger Report der aktuell gefilterten Projekte.",
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
                    # Fälle werden behalten, nicht verworfen). pandas macht aus einem fehlenden
                    # Wert in einer gemischten Objekt-Spalte float("nan") statt None - "nan or
                    # fallback" liefert dann fälschlich "nan" zurück (nan ist wahr), deshalb
                    # explizit mit pd.isna() statt truthy-Check prüfen.
                    bezeichnung = df.loc[i, "projektbezeichnung"]
                    if pd.isna(bezeichnung) or not bezeichnung:
                        bezeichnung = "(ohne Bezeichnung)"
                    praefix = f"{PRUEF_BADGE} " if df.loc[i, "pruefen"] == PRUEF_BADGE else ""
                    return f"{praefix}{bezeichnung} ({df.loc[i, 'ort']})"

                auswahl = st.selectbox("Projekt", gefiltert.index, format_func=_projekt_label)
                projekt_id = int(df.loc[auswahl, "id"])
                with closing(database.connect(active_settings)) as conn:
                    vorkommen = database.list_vorlagen_fuer_projekt(conn, projekt_id)
                    statusverlauf = database.list_status_log_fuer_projekt(conn, projekt_id)
                if vorkommen:
                    vorkommen_df = pd.DataFrame([dict(r) for r in vorkommen])
                    spalten = ["gremium", "sitzungsdatum", "vorlagen_nr", "sitzung_url"]
                    st.dataframe(vorkommen_df[spalten], hide_index=True, use_container_width=True)
                else:
                    st.write("Keine Vorkommen gefunden.")

                with st.expander(f"Statusverlauf ({len(statusverlauf)})"):
                    if statusverlauf:
                        verlauf_df = pd.DataFrame([dict(r) for r in statusverlauf])
                        verlauf_df = verlauf_df.rename(
                            columns={
                                "zeitpunkt": "Zeitpunkt",
                                "alter_status": "Alter Status",
                                "neuer_status": "Neuer Status",
                                "notiz": "Notiz",
                            }
                        )
                        st.dataframe(
                            verlauf_df[["Zeitpunkt", "Alter Status", "Neuer Status", "Notiz"]],
                            hide_index=True,
                            use_container_width=True,
                        )
                    else:
                        st.caption("Kein Statusverlauf vorhanden.")

    with tab_quellen:
        if not quellen_rows:
            st.info("Noch keine Quelle gelaufen.")
        else:
            quellen_df = pd.DataFrame([dict(r) for r in quellen_rows])
            st.dataframe(quellen_df, hide_index=True, use_container_width=True)

    if aktiv or web_aktiv:
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
