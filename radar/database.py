"""SQLite-Persistenz: Sitzungen, Vorlagen, Projekte, Status (Schritt 4).

Schema siehe CLAUDE.md "Datenmodell": `quellen` (Zustand/Monitoring), `sitzungen`, `vorlagen`,
`dokumente`, `projekte`, `projekt_vorlage` (n:m), `status_log`. `PRAGMA journal_mode=WAL`,
`foreign_keys=ON`, Schema-Version in `PRAGMA user_version` mit kleinen SQL-Migrationen.

Dedup, zweistufig:
  1. Vorlage: `quelle_id` + externe Vorlagen-ID + Dokument-Hash. Unverändert -> derselbe Datensatz,
     `upsert_vorlage()` liefert `ist_neu=False` und der Aufrufer kann die teure Analyse überspringen.
     Ändert sich eine Anlage (neuer Hash), entsteht ein neuer `vorlagen`-Datensatz -> erneute Analyse.
  2. Projekt: `upsert_projekt()` sucht über eine normalisierte Adresse/Bezeichnung
     (`normalize_projekt_key`) innerhalb derselben Quelle. Kein eindeutiger Treffer -> neuer
     Datensatz statt Rateverknüpfung (CLAUDE.md: "im Zweifel manuell zusammenführen").

Manuell gepflegte Felder (`status`, `notizen`) werden von `upsert_projekt()` nie angefasst — nur
`set_projekt_status()`/`set_projekt_notizen()` (für das künftige Dashboard) schreiben sie, und
Statuswechsel werden in `status_log` protokolliert.

Dieses Modul ist bewusst von `radar.scraper`/`radar.parser` entkoppelt: es nimmt deren
Domänentypen (`Sitzung`, `Dokument`, `ProjektExtraktion`, `SourceRunResult`) entgegen, ruft sie
aber nicht selbst auf. Die Verdrahtung (Lauf -> Analyse -> Speichern) ist ein späterer Schritt.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from radar.config import Settings, Source

if TYPE_CHECKING:
    from radar.parser import AnalyseErgebnis, ProjektExtraktion
    from radar.scraper import SourceRunResult
    from radar.sources.base import Dokument, Sitzung

logger = logging.getLogger(__name__)

_SCHEMA_V1 = """
CREATE TABLE quellen (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    bundesland TEXT NOT NULL,
    system TEXT NOT NULL,
    letzter_lauf TEXT,
    letzter_erfolg TEXT,
    letzter_fehler TEXT,
    letzter_fehler_text TEXT,
    anzahl_sitzungen INTEGER NOT NULL DEFAULT 0,
    anzahl_vorlagen INTEGER NOT NULL DEFAULT 0,
    anzahl_dokumente INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sitzungen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quelle_id TEXT NOT NULL REFERENCES quellen(id),
    external_id TEXT NOT NULL,
    gremium TEXT NOT NULL,
    datum TEXT NOT NULL,
    uhrzeit TEXT,
    ort TEXT,
    url TEXT,
    abgesagt INTEGER NOT NULL DEFAULT 0,
    UNIQUE (quelle_id, external_id)
);

CREATE TABLE vorlagen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quelle_id TEXT NOT NULL REFERENCES quellen(id),
    sitzung_id INTEGER NOT NULL REFERENCES sitzungen(id),
    external_id TEXT NOT NULL,
    vorlagen_nr TEXT,
    dokument_hash TEXT NOT NULL,
    analyse_status TEXT NOT NULL DEFAULT 'neu',
    analysiert_am TEXT,
    UNIQUE (quelle_id, external_id, dokument_hash)
);

CREATE TABLE dokumente (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quelle_id TEXT NOT NULL REFERENCES quellen(id),
    sitzung_id INTEGER NOT NULL REFERENCES sitzungen(id),
    vorlage_id INTEGER REFERENCES vorlagen(id),
    external_id TEXT NOT NULL,
    titel TEXT NOT NULL,
    typ TEXT NOT NULL,
    url TEXT NOT NULL,
    sha256 TEXT,
    dateipfad TEXT,
    UNIQUE (quelle_id, external_id)
);

CREATE TABLE projekte (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bundesland TEXT NOT NULL,
    kommune TEXT NOT NULL,
    quelle_id TEXT NOT NULL REFERENCES quellen(id),
    dedup_schluessel TEXT NOT NULL,
    projektbezeichnung TEXT,
    strasse TEXT,
    hausnummer TEXT,
    flurstueck TEXT,
    ort TEXT,
    we_gesamt INTEGER,
    we_miete INTEGER,
    we_gefoerdert_anzahl INTEGER,
    we_gefoerdert INTEGER,
    quote_gefoerdert REAL,
    ausfuehrungszeitraum TEXT,
    antragsteller TEXT,
    kurzfassung TEXT,
    wohnform TEXT NOT NULL DEFAULT 'UNKLAR',
    verfahrensstand TEXT NOT NULL DEFAULT 'UNKLAR',
    konfidenz REAL NOT NULL DEFAULT 0,
    evidenz TEXT,
    seite INTEGER,
    status TEXT NOT NULL DEFAULT 'Neu',
    notizen TEXT NOT NULL DEFAULT '',
    erstellt_am TEXT NOT NULL,
    aktualisiert_am TEXT NOT NULL,
    UNIQUE (quelle_id, dedup_schluessel)
);

CREATE TABLE projekt_vorlage (
    projekt_id INTEGER NOT NULL REFERENCES projekte(id),
    vorlage_id INTEGER NOT NULL REFERENCES vorlagen(id),
    PRIMARY KEY (projekt_id, vorlage_id)
);

CREATE TABLE status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    projekt_id INTEGER NOT NULL REFERENCES projekte(id),
    zeitpunkt TEXT NOT NULL,
    alter_status TEXT,
    neuer_status TEXT NOT NULL,
    notiz TEXT
);

CREATE INDEX idx_sitzungen_quelle ON sitzungen(quelle_id);
CREATE INDEX idx_vorlagen_sitzung ON vorlagen(sitzung_id);
CREATE INDEX idx_dokumente_vorlage ON dokumente(vorlage_id);
CREATE INDEX idx_projekte_status ON projekte(status);
"""

# V2: Fehlversuche je Vorlage zählen, damit eine Vorlage, deren Analyse immer wieder scheitert
# (z. B. ein Dokument, an dem das Modell dauerhaft keine gültige Antwort liefert), nicht in jedem
# künftigen Lauf erneut LLM-Budget verbraucht (siehe `upsert_vorlage`, llm.max_fehlversuche_pro_vorlage).
_SCHEMA_V2 = """
ALTER TABLE vorlagen ADD COLUMN analyse_fehlversuche INTEGER NOT NULL DEFAULT 0;
"""

_MIGRATIONS: list[str] = [_SCHEMA_V1, _SCHEMA_V2]


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in enumerate(_MIGRATIONS, start=1):
        if version > current:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def connect(settings: Settings) -> sqlite3.Connection:
    """Öffnet (und legt bei Bedarf an) die SQLite-Datenbank aus `settings.database.path`."""
    path = settings.resolve_path(settings.database.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL erlaubt gleichzeitiges Lesen+Schreiben, aber nicht zwei gleichzeitige Schreiber
    # (Dashboard-Subprozess und Scheduler-Dienst greifen auf dieselbe Datei zu, siehe
    # docker-compose.yml) - ohne busy_timeout wirft ein kollidierender Schreibzugriff sofort
    # "database is locked" statt kurz zu warten.
    conn.execute("PRAGMA busy_timeout = 5000")
    _migrate(conn)
    return conn


# -- Quellen-Monitoring ---------------------------------------------------------------------------


def ensure_quelle(conn: sqlite3.Connection, source: Source) -> None:
    """Stellt sicher, dass eine `quellen`-Zeile existiert, *bevor* eine Quelle verarbeitet wird
    (Fremdschlüssel-Voraussetzung für `sitzungen`/`vorlagen`/`projekte`). Rührt anders als
    `record_source_run()` keine Monitoring-Felder an - die schreibt erst der fertige Lauf."""
    conn.execute(
        "INSERT OR IGNORE INTO quellen (id, name, bundesland, system) VALUES (?, ?, ?, ?)",
        (source.id, source.name, source.bundesland, source.system),
    )
    conn.commit()


def record_source_run(
    conn: sqlite3.Connection, source: Source, result: SourceRunResult, *, when: datetime | None = None
) -> None:
    """Zustand einer Quelle nach einem Lauf festhalten (siehe CLAUDE.md "Quellen-Monitoring").
    `letzter_erfolg`/`letzter_fehler` werden nur bei tatsächlichem Erfolg/Fehler aktualisiert,
    der jeweils andere Zeitstempel bleibt vom letzten Mal erhalten."""
    when = when or datetime.now(UTC)
    now_iso = when.isoformat()
    ok = result.ok
    conn.execute(
        """
        INSERT INTO quellen (
            id, name, bundesland, system, letzter_lauf, letzter_erfolg, letzter_fehler,
            letzter_fehler_text, anzahl_sitzungen, anzahl_vorlagen, anzahl_dokumente
        ) VALUES (
            :id, :name, :bundesland, :system, :jetzt, :erfolg, :fehler, :fehler_text, :sitzungen, :vorlagen, :dokumente
        )
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            bundesland = excluded.bundesland,
            system = excluded.system,
            letzter_lauf = excluded.letzter_lauf,
            letzter_erfolg = COALESCE(excluded.letzter_erfolg, quellen.letzter_erfolg),
            letzter_fehler = COALESCE(excluded.letzter_fehler, quellen.letzter_fehler),
            letzter_fehler_text = COALESCE(excluded.letzter_fehler_text, quellen.letzter_fehler_text),
            anzahl_sitzungen = excluded.anzahl_sitzungen,
            anzahl_vorlagen = excluded.anzahl_vorlagen,
            anzahl_dokumente = excluded.anzahl_dokumente
        """,
        {
            "id": source.id,
            "name": source.name,
            "bundesland": source.bundesland,
            "system": source.system,
            "jetzt": now_iso,
            "erfolg": now_iso if ok else None,
            "fehler": None if ok else now_iso,
            "fehler_text": None if ok else "; ".join(result.fehler),
            "sitzungen": result.sitzungen,
            "vorlagen": result.vorlagen,
            "dokumente": result.dokumente,
        },
    )
    conn.commit()


# -- Sitzungen / Dokumente ------------------------------------------------------------------------


def upsert_sitzung(conn: sqlite3.Connection, quelle_id: str, sitzung: Sitzung) -> int:
    conn.execute(
        """
        INSERT INTO sitzungen (quelle_id, external_id, gremium, datum, uhrzeit, ort, url, abgesagt)
        VALUES (:quelle_id, :external_id, :gremium, :datum, :uhrzeit, :ort, :url, :abgesagt)
        ON CONFLICT(quelle_id, external_id) DO UPDATE SET
            gremium = excluded.gremium, datum = excluded.datum, uhrzeit = excluded.uhrzeit,
            ort = excluded.ort, url = excluded.url, abgesagt = excluded.abgesagt
        """,
        {
            "quelle_id": quelle_id,
            "external_id": sitzung.external_id,
            "gremium": sitzung.gremium,
            "datum": sitzung.datum.isoformat(),
            "uhrzeit": sitzung.uhrzeit,
            "ort": sitzung.ort,
            "url": sitzung.url,
            "abgesagt": int(sitzung.abgesagt),
        },
    )
    row = conn.execute(
        "SELECT id FROM sitzungen WHERE quelle_id = ? AND external_id = ?", (quelle_id, sitzung.external_id)
    ).fetchone()
    conn.commit()
    return row["id"]


def upsert_dokument(
    conn: sqlite3.Connection, quelle_id: str, sitzung_id: int, vorlage_id: int | None, dokument: Dokument
) -> int:
    conn.execute(
        """
        INSERT INTO dokumente (quelle_id, sitzung_id, vorlage_id, external_id, titel, typ, url)
        VALUES (:quelle_id, :sitzung_id, :vorlage_id, :external_id, :titel, :typ, :url)
        ON CONFLICT(quelle_id, external_id) DO UPDATE SET
            sitzung_id = excluded.sitzung_id, vorlage_id = excluded.vorlage_id,
            titel = excluded.titel, typ = excluded.typ, url = excluded.url
        """,
        {
            "quelle_id": quelle_id,
            "sitzung_id": sitzung_id,
            "vorlage_id": vorlage_id,
            "external_id": dokument.external_id,
            "titel": dokument.titel,
            "typ": dokument.typ,
            "url": dokument.url,
        },
    )
    row = conn.execute(
        "SELECT id FROM dokumente WHERE quelle_id = ? AND external_id = ?", (quelle_id, dokument.external_id)
    ).fetchone()
    conn.commit()
    return row["id"]


def mark_dokument_heruntergeladen(conn: sqlite3.Connection, dokument_id: int, sha256: str, dateipfad: str) -> None:
    conn.execute("UPDATE dokumente SET sha256 = ?, dateipfad = ? WHERE id = ?", (sha256, dateipfad, dokument_id))
    conn.commit()


# -- Vorlagen (Dedup Stufe 1: Dokument-Hash) -------------------------------------------------------


# Status einer `vorlagen`-Zeile, die noch (erneut) analysiert werden muss: 'neu' (noch nie versucht)
# sowie die beiden Fälle, in denen die letzte Analyse nicht wirklich abgeschlossen wurde und ein
# künftiger Lauf (mit dann vielleicht wieder freiem Budget) es erneut versuchen soll. RELEVANT,
# NICHT_RELEVANT und OCR_NOETIG gelten dagegen als abgeschlossen und werden nicht wiederholt.
_UNSETTLED_ANALYSE_STATUS = {"neu", "FEHLER", "BUDGET_ERSCHOEPFT"}


def upsert_vorlage(
    conn: sqlite3.Connection,
    quelle_id: str,
    sitzung_id: int,
    external_id: str,
    vorlagen_nr: str | None,
    dokument_hash: str,
    max_fehlversuche: int = 3,
) -> tuple[int, bool]:
    """Liefert `(vorlage_id, braucht_analyse)`. Ist dieser Dokument-Hash für diese Vorlage bereits
    gespeichert (unverändert seit dem letzten Lauf) UND war die letzte Analyse abgeschlossen
    (nicht nur an einem Budget oder Fehler gescheitert), ist `braucht_analyse=False` - der
    Aufrufer kann die LLM-Analyse überspringen. Ändert sich eine Anlage (neuer Hash), entsteht ein
    neuer Datensatz.

    Ist die Analyse bereits `max_fehlversuche`-mal mit `FEHLER` gescheitert, wird sie ebenfalls
    nicht mehr versucht (sonst kostete eine dauerhaft scheiternde Vorlage in jedem Lauf erneut
    Geld). `BUDGET_ERSCHOEPFT` zählt nicht als Fehlversuch - da wurde gar nicht erst angefragt
    oder die Notbremse hat (z. B. wegen leerem Guthaben) gegriffen."""
    existing = conn.execute(
        "SELECT id, analyse_status, analyse_fehlversuche FROM vorlagen "
        "WHERE quelle_id = ? AND external_id = ? AND dokument_hash = ?",
        (quelle_id, external_id, dokument_hash),
    ).fetchone()
    if existing is not None:
        braucht_analyse = existing["analyse_status"] in _UNSETTLED_ANALYSE_STATUS
        if existing["analyse_status"] == "FEHLER" and existing["analyse_fehlversuche"] >= max_fehlversuche:
            logger.warning(
                "Vorlage %s (Quelle '%s') nach %d Fehlversuchen aufgegeben, keine weiteren LLM-Anfragen",
                external_id,
                quelle_id,
                existing["analyse_fehlversuche"],
            )
            braucht_analyse = False
        return existing["id"], braucht_analyse

    cur = conn.execute(
        """
        INSERT INTO vorlagen (quelle_id, sitzung_id, external_id, vorlagen_nr, dokument_hash, analyse_status)
        VALUES (?, ?, ?, ?, ?, 'neu')
        """,
        (quelle_id, sitzung_id, external_id, vorlagen_nr, dokument_hash),
    )
    conn.commit()
    return cur.lastrowid, True


# -- Projekte (Dedup Stufe 2: normalisierte Adresse/Bezeichnung) -------------------------------------


def normalize_projekt_key(extraktion: ProjektExtraktion) -> str:
    """Schlüssel für die Projekt-Dedup innerhalb einer Quelle: normalisierte Adresse, sonst
    Projektbezeichnung, sonst Flurstück, sonst Kurzfassung. Fehlt alles, wird ein zufälliger
    Schlüssel erzeugt statt leerer String - sonst würden verschiedene "namenlose" Projekte
    fälschlich zu einem einzigen Datensatz verschmelzen (CLAUDE.md: im Zweifel nicht raten)."""
    a = extraktion.adresse
    adresse = " ".join(t for t in (a.strasse, a.hausnummer, a.ort) if t)
    basis = adresse or extraktion.projektbezeichnung or extraktion.adresse.flurstueck or extraktion.kurzfassung
    if not basis:
        basis = f"unbenannt-{uuid4().hex}"
    normalisiert = re.sub(r"[^\w\s]", "", basis.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalisiert).strip()


def _extraktion_felder(extraktion: ProjektExtraktion) -> dict:
    return {
        "projektbezeichnung": extraktion.projektbezeichnung,
        "strasse": extraktion.adresse.strasse,
        "hausnummer": extraktion.adresse.hausnummer,
        "flurstueck": extraktion.adresse.flurstueck,
        "ort": extraktion.adresse.ort,
        "we_gesamt": extraktion.we_gesamt,
        "we_miete": extraktion.we_miete,
        "we_gefoerdert_anzahl": extraktion.we_gefoerdert_anzahl,
        "we_gefoerdert": None if extraktion.we_gefoerdert is None else int(extraktion.we_gefoerdert),
        "quote_gefoerdert": extraktion.quote_gefoerdert,
        "ausfuehrungszeitraum": extraktion.ausfuehrungszeitraum,
        "antragsteller": extraktion.antragsteller,
        "kurzfassung": extraktion.kurzfassung,
        "wohnform": extraktion.wohnform.value,
        "verfahrensstand": extraktion.verfahrensstand.value,
        "konfidenz": extraktion.konfidenz,
        "evidenz": extraktion.evidenz,
        "seite": extraktion.seite,
    }


def upsert_projekt(
    conn: sqlite3.Connection,
    *,
    quelle_id: str,
    bundesland: str,
    kommune: str,
    extraktion: ProjektExtraktion,
    when: datetime | None = None,
) -> tuple[int, bool]:
    """Legt ein Projekt an oder aktualisiert die extrahierten Felder eines bestehenden (per
    `normalize_projekt_key` gefundenen) Projekts. `status` und `notizen` werden nie angefasst -
    die pflegt ausschließlich `set_projekt_status()`/`set_projekt_notizen()`.

    Liefert `(projekt_id, ist_neu)` - `ist_neu` unterscheidet Neuanlage von Aktualisierung eines
    bestehenden Projekts (Muster wie `upsert_vorlage()`s `(vorlage_id, braucht_analyse)`), damit
    `radar.scraper` daraus die E-Mail-Benachrichtigung (`radar/notifier.py`) speisen kann: nur
    wirklich neue Treffer sollen gemeldet werden, keine bloße Aktualisierung bekannter WE-Zahlen."""
    when = when or datetime.now(UTC)
    now_iso = when.isoformat()
    schluessel = normalize_projekt_key(extraktion)
    felder = _extraktion_felder(extraktion)

    existing = conn.execute(
        "SELECT id FROM projekte WHERE quelle_id = ? AND dedup_schluessel = ?", (quelle_id, schluessel)
    ).fetchone()

    if existing is not None:
        projekt_id = existing["id"]
        conn.execute(
            """
            UPDATE projekte SET
                projektbezeichnung=:projektbezeichnung, strasse=:strasse, hausnummer=:hausnummer,
                flurstueck=:flurstueck, ort=:ort, we_gesamt=:we_gesamt, we_miete=:we_miete,
                we_gefoerdert_anzahl=:we_gefoerdert_anzahl, we_gefoerdert=:we_gefoerdert,
                quote_gefoerdert=:quote_gefoerdert, ausfuehrungszeitraum=:ausfuehrungszeitraum,
                antragsteller=:antragsteller, kurzfassung=:kurzfassung, wohnform=:wohnform,
                verfahrensstand=:verfahrensstand, konfidenz=:konfidenz, evidenz=:evidenz,
                seite=:seite, aktualisiert_am=:jetzt
            WHERE id = :id
            """,
            {**felder, "jetzt": now_iso, "id": projekt_id},
        )
        conn.commit()
        return projekt_id, False

    cur = conn.execute(
        """
        INSERT INTO projekte (
            bundesland, kommune, quelle_id, dedup_schluessel,
            projektbezeichnung, strasse, hausnummer, flurstueck, ort,
            we_gesamt, we_miete, we_gefoerdert_anzahl, we_gefoerdert, quote_gefoerdert,
            ausfuehrungszeitraum, antragsteller, kurzfassung, wohnform, verfahrensstand,
            konfidenz, evidenz, seite, status, notizen, erstellt_am, aktualisiert_am
        ) VALUES (
            :bundesland, :kommune, :quelle_id, :dedup_schluessel,
            :projektbezeichnung, :strasse, :hausnummer, :flurstueck, :ort,
            :we_gesamt, :we_miete, :we_gefoerdert_anzahl, :we_gefoerdert, :quote_gefoerdert,
            :ausfuehrungszeitraum, :antragsteller, :kurzfassung, :wohnform, :verfahrensstand,
            :konfidenz, :evidenz, :seite, 'Neu', '', :jetzt, :jetzt
        )
        """,
        {
            **felder,
            "bundesland": bundesland,
            "kommune": kommune,
            "quelle_id": quelle_id,
            "dedup_schluessel": schluessel,
            "jetzt": now_iso,
        },
    )
    projekt_id = cur.lastrowid
    _log_status(conn, projekt_id, None, "Neu", "automatisch angelegt", when)
    conn.commit()
    return projekt_id, True


def speichere_analyse_ergebnis(
    conn: sqlite3.Connection,
    *,
    vorlage_id: int,
    quelle_id: str,
    bundesland: str,
    kommune: str,
    ergebnis: AnalyseErgebnis,
    when: datetime | None = None,
) -> tuple[int, bool] | None:
    """Analyseergebnis einer Vorlage speichern. Bei `AnalyseStatus.RELEVANT` wird zusätzlich das
    Projekt angelegt/aktualisiert (Dedup) und über `projekt_vorlage` mit dieser Vorlage verknüpft.
    Liefert `(projekt_id, ist_neu)` (siehe `upsert_projekt()`), oder `None` wenn die Vorlage nicht
    relevant war."""
    when = when or datetime.now(UTC)
    conn.execute(
        """
        UPDATE vorlagen
        SET analyse_status = ?, analysiert_am = ?,
            analyse_fehlversuche = analyse_fehlversuche + CASE WHEN ? = 'FEHLER' THEN 1 ELSE 0 END
        WHERE id = ?
        """,
        (ergebnis.status.value, when.isoformat(), ergebnis.status.value, vorlage_id),
    )
    conn.commit()

    if ergebnis.extraktion is None:
        return None

    projekt_id, ist_neu = upsert_projekt(
        conn, quelle_id=quelle_id, bundesland=bundesland, kommune=kommune, extraktion=ergebnis.extraktion, when=when
    )
    conn.execute(
        "INSERT OR IGNORE INTO projekt_vorlage (projekt_id, vorlage_id) VALUES (?, ?)", (projekt_id, vorlage_id)
    )
    conn.commit()
    return projekt_id, ist_neu


# -- Statuspflege (manuell, nie von einem Lauf überschrieben) ---------------------------------------


def _log_status(
    conn: sqlite3.Connection, projekt_id: int, alter_status: str | None, neuer_status: str, notiz: str, when: datetime
) -> None:
    conn.execute(
        "INSERT INTO status_log (projekt_id, zeitpunkt, alter_status, neuer_status, notiz) VALUES (?, ?, ?, ?, ?)",
        (projekt_id, when.isoformat(), alter_status, neuer_status, notiz),
    )


def set_projekt_status(
    conn: sqlite3.Connection, projekt_id: int, neuer_status: str, *, notiz: str = "", when: datetime | None = None
) -> None:
    when = when or datetime.now(UTC)
    row = conn.execute("SELECT status FROM projekte WHERE id = ?", (projekt_id,)).fetchone()
    if row is None:
        raise ValueError(f"Projekt {projekt_id} existiert nicht")
    conn.execute(
        "UPDATE projekte SET status = ?, aktualisiert_am = ? WHERE id = ?",
        (neuer_status, when.isoformat(), projekt_id),
    )
    _log_status(conn, projekt_id, row["status"], neuer_status, notiz, when)
    conn.commit()


def set_projekt_notizen(conn: sqlite3.Connection, projekt_id: int, notizen: str) -> None:
    conn.execute("UPDATE projekte SET notizen = ? WHERE id = ?", (notizen, projekt_id))
    conn.commit()


# -- Abfragen (Grundlage für das Dashboard, Schritt 5) -----------------------------------------------


def list_projekte(conn: sqlite3.Connection, *, status: str | None = None) -> list[sqlite3.Row]:
    if status is not None:
        return conn.execute(
            "SELECT * FROM projekte WHERE status = ? ORDER BY aktualisiert_am DESC", (status,)
        ).fetchall()
    return conn.execute("SELECT * FROM projekte ORDER BY aktualisiert_am DESC").fetchall()


def list_quellen(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM quellen ORDER BY name").fetchall()


def list_vorlagen_fuer_projekt(conn: sqlite3.Connection, projekt_id: int) -> list[sqlite3.Row]:
    """Alle Sitzungen/Vorlagen, in denen ein Projekt vorkam (CLAUDE.md: ein Projekt taucht in
    mehreren Gremien und Sitzungen auf)."""
    return conn.execute(
        """
        SELECT v.*, s.gremium, s.datum AS sitzungsdatum, s.url AS sitzung_url
        FROM projekt_vorlage pv
        JOIN vorlagen v ON v.id = pv.vorlage_id
        JOIN sitzungen s ON s.id = v.sitzung_id
        WHERE pv.projekt_id = ?
        ORDER BY s.datum
        """,
        (projekt_id,),
    ).fetchall()


def list_status_log_fuer_projekt(conn: sqlite3.Connection, projekt_id: int) -> list[sqlite3.Row]:
    """Statusverlauf eines Projekts, älteste Änderung zuerst (Dashboard: Aufklapp-Bereich in der
    Detailansicht). `_log_status()` schreibt hier hinein - bei Neuanlage (`upsert_projekt()`) und
    bei jeder manuellen Statusänderung (`set_projekt_status()`)."""
    return conn.execute(
        "SELECT * FROM status_log WHERE projekt_id = ? ORDER BY zeitpunkt", (projekt_id,)
    ).fetchall()
