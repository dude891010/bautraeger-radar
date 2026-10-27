"""E-Mail-Benachrichtigung bei neuen Treffern (Schritt 7, Alerting, CLAUDE.md: "optional E-Mail
bei neuen Treffern").

Konfiguration ausschließlich über Umgebungsvariablen (`.env`, wie `ANTHROPIC_API_KEY`/
`SCRAPER_CONTACT`): `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`NOTIFICATION_RECIPIENT`. Bewusst **optional**: fehlt eine der Variablen, bleibt die
Benachrichtigung still deaktiviert (`load_smtp_config()` liefert `None`), kein Fehler und kein
abgebrochener Lauf - E-Mail-Versand ist ein Nice-to-have, kein Kernbestandteil (CLAUDE.md:
"Quellen sind voneinander isoliert", derselbe Grundsatz gilt hier für den Versand selbst).

Netzwerk (SMTP) ist hinter `send_notification()`/einer injizierbaren `smtp_client_factory`
versteckt, Tests ersetzen sie durch ein Fake - kein echter SMTP-Versand in `pytest`.

Nimmt STARTTLS auf dem konfigurierten Port an (Normalfall bei Port 587). Für einen SMTP-Server
mit implizitem TLS (Port 465, `SMTP_SSL`) müsste `_default_smtp_client` angepasst werden - im
Zielbetrieb (Firmen-IT-Mailrelay) noch nicht verifiziert.
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

try:  # .env nur laden, wenn python-dotenv installiert ist (lokale Entwicklung) - wie radar/config.py
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from radar.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT_SECONDS = 10  # CLAUDE.md: "nie ein Request ohne Timeout" - gilt sinngemäß auch für SMTP


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    user: str
    password: str
    recipients: tuple[str, ...]  # NOTIFICATION_RECIPIENT: eine oder mehrere, kommagetrennte Adressen


@dataclass(frozen=True)
class NeuerTreffer:
    """Ein neu gefundenes, relevantes Bauvorhaben - vom Aufrufer (radar/scraper.py) aus
    `ProjektExtraktion` + Herkunft zusammengestellt, absichtlich schlank (nur, was in die
    Zusammenfassungs-Mail gehört)."""

    kommune: str
    titel: str
    we_gesamt: int | None
    url: str | None


@dataclass(frozen=True)
class NeuerWebFund:
    """Ein neuer Fund der Web-Recherche (radar/web_recherche.py, Phase 3/4) - eigener Abschnitt in
    der Mail, weil ein Presseartikel kein ausgewertetes Ratsdokument ist (keine LLM-Prüfung, WE-Zahl
    nur, wenn sie wörtlich im Text steht)."""

    ort: str
    phase: str  # lesbare Bezeichnung, z. B. "Phase 4 – Baubeginn"
    titel: str
    we_anzahl: int | None
    entfernung_km: float
    url: str


def load_smtp_config(env: dict[str, str] | None = None) -> SMTPConfig | None:
    """Liest `SMTP_SERVER`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`NOTIFICATION_RECIPIENT` aus
    der Umgebung (`env`-Parameter nur für Tests, sonst `os.environ`). `NOTIFICATION_RECIPIENT`
    darf eine oder mehrere, durch Komma getrennte Adressen enthalten (z. B. Vertrieb und IT).
    Liefert `None`, wenn eine Pflichtvariable fehlt, kein gültiger Empfänger übrig bleibt oder
    `SMTP_PORT` keine Zahl ist - die Benachrichtigung ist dann bewusst deaktiviert statt einen
    Lauf abzubrechen."""
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")  # wie radar/config.load_settings(): .env in os.environ laden
    quelle = env if env is not None else os.environ

    host = quelle.get("SMTP_SERVER", "").strip()
    user = quelle.get("SMTP_USER", "").strip()
    password = quelle.get("SMTP_PASSWORD", "").strip()
    recipient_raw = quelle.get("NOTIFICATION_RECIPIENT", "").strip()
    port_raw = (quelle.get("SMTP_PORT") or "587").strip()  # leerer Wert wie ein fehlender behandeln

    recipients = tuple(r.strip() for r in recipient_raw.split(",") if r.strip())
    if not (host and user and password and recipients):
        return None
    try:
        port = int(port_raw)
    except ValueError:
        logger.warning("SMTP_PORT '%s' ist keine Zahl, Benachrichtigung deaktiviert", port_raw)
        return None
    return SMTPConfig(host=host, port=port, user=user, password=password, recipients=recipients)


def _link_cell(url: str | None) -> str:
    if not url:
        return "-"
    return f'<a href="{escape(url)}">Quelle öffnen</a>'


_TD = "padding:6px 10px;border-bottom:1px solid #ddd;"


def _betreff(treffer: list[NeuerTreffer], web_funde: list[NeuerWebFund]) -> str:
    teile = []
    if treffer:
        teile.append(f"{len(treffer)} neues Vorhaben" if len(treffer) == 1 else f"{len(treffer)} neue Vorhaben")
    if web_funde:
        teile.append(f"{len(web_funde)} neue(r) Web-Fund(e)")
    return "Bauträger-Radar: " + ", ".join(teile)


def _web_html(web_funde: list[NeuerWebFund]) -> str:
    if not web_funde:
        return ""
    zeilen = "\n".join(
        "<tr>"
        f"<td style='{_TD}'>{escape(f.phase)}</td>"
        f"<td style='{_TD}'>{escape(f.ort)} ({f.entfernung_km:.0f} km)</td>"
        f"<td style='{_TD}'>{escape(f.titel)}</td>"
        f"<td style='{_TD}text-align:right;'>{f.we_anzahl if f.we_anzahl is not None else '?'}</td>"
        f"<td style='{_TD}'>{_link_cell(f.url)}</td>"
        "</tr>"
        for f in web_funde
    )
    return f"""
  <h3 style="margin-bottom:4px;">Web-Recherche: Baugenehmigungen &amp; Baubeginne</h3>
  <p style="color:#555;margin-top:0;">Presse-/Webfunde im Umkreis, per Stichwort eingeordnet (ohne KI-Prüfung) -
  WE-Zahl nur, wenn sie im Text steht. Bitte Quelle prüfen.</p>
  <table style="border-collapse:collapse;width:100%;max-width:800px;">
    <thead>
      <tr style="background:#f2f2f2;text-align:left;">
        <th style="padding:6px 10px;">Phase</th>
        <th style="padding:6px 10px;">Ort</th>
        <th style="padding:6px 10px;">Titel</th>
        <th style="padding:6px 10px;text-align:right;">WE</th>
        <th style="padding:6px 10px;">Quelle</th>
      </tr>
    </thead>
    <tbody>
      {zeilen}
    </tbody>
  </table>"""


def _build_html(treffer: list[NeuerTreffer], web_funde: list[NeuerWebFund] | None = None) -> str:
    web_funde = web_funde or []
    zeilen = "\n".join(
        "<tr>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;'>{escape(t.kommune)}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;'>{escape(t.titel)}</td>"
        "<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right;'>"
        f"{t.we_gesamt if t.we_gesamt is not None else '?'}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;'>{_link_cell(t.url)}</td>"
        "</tr>"
        for t in treffer
    )
    anzahl = len(treffer)
    ueberschrift = f"{anzahl} neues Vorhaben" if anzahl == 1 else f"{anzahl} neue Vorhaben"
    if not treffer:
        return f"""\
<html>
<body style="font-family: Arial, Helvetica, sans-serif; color:#1a1a1a;">
  <h2 style="margin-bottom:4px;">{escape(_betreff(treffer, web_funde))}</h2>{_web_html(web_funde)}
</body>
</html>
"""
    return f"""\
<html>
<body style="font-family: Arial, Helvetica, sans-serif; color:#1a1a1a;">
  <h2 style="margin-bottom:4px;">Bauträger-Radar: {ueberschrift}</h2>
  <p style="color:#555;margin-top:0;">Neu erkannte Miet-/geförderte Wohnbauprojekte im aktuellen Lauf.</p>
  <table style="border-collapse:collapse;width:100%;max-width:800px;">
    <thead>
      <tr style="background:#f2f2f2;text-align:left;">
        <th style="padding:6px 10px;">Kommune</th>
        <th style="padding:6px 10px;">Vorhaben</th>
        <th style="padding:6px 10px;text-align:right;">WE</th>
        <th style="padding:6px 10px;">Quelle</th>
      </tr>
    </thead>
    <tbody>
      {zeilen}
    </tbody>
  </table>{_web_html(web_funde)}
</body>
</html>
"""


def _build_text(treffer: list[NeuerTreffer], web_funde: list[NeuerWebFund] | None = None) -> str:
    web_funde = web_funde or []
    teile = [_betreff(treffer, web_funde)]
    if treffer:
        teile.append(
            "\n".join(
                f"- {t.kommune}: {t.titel} ({t.we_gesamt if t.we_gesamt is not None else '?'} WE)"
                f"{' - ' + t.url if t.url else ''}"
                for t in treffer
            )
        )
    if web_funde:
        teile.append(
            "Web-Recherche (Baugenehmigungen & Baubeginne, ohne KI-Prüfung):\n"
            + "\n".join(
                f"- {f.phase} | {f.ort} ({f.entfernung_km:.0f} km): {f.titel} "
                f"({f.we_anzahl if f.we_anzahl is not None else '?'} WE) - {f.url}"
                for f in web_funde
            )
        )
    return "\n\n".join(teile)


def build_message(
    config: SMTPConfig, treffer: list[NeuerTreffer], web_funde: list[NeuerWebFund] | None = None
) -> MIMEMultipart:
    """HTML-E-Mail (mit Text-Alternative fürs Spam-Filtering/Clients ohne HTML) für `treffer` und,
    in einem eigenen Abschnitt, neue Web-Funde."""
    web_funde = web_funde or []
    msg = MIMEMultipart("alternative")
    msg["Subject"] = _betreff(treffer, web_funde)
    msg["From"] = config.user
    msg["To"] = ", ".join(config.recipients)  # RFC 5322: mehrere Adressen im To-Header kommagetrennt
    msg.attach(MIMEText(_build_text(treffer, web_funde), "plain", "utf-8"))
    msg.attach(MIMEText(_build_html(treffer, web_funde), "html", "utf-8"))
    return msg


def _default_smtp_client(host: str, port: int) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS)


def send_notification(
    treffer: list[NeuerTreffer],
    config: SMTPConfig | None = None,
    *,
    web_funde: list[NeuerWebFund] | None = None,
    smtp_client_factory: Callable[[str, int], smtplib.SMTP] = _default_smtp_client,
) -> bool:
    """Sendet eine Zusammenfassungs-Mail für `treffer`. Liefert `False` ohne zu senden, wenn SMTP
    nicht konfiguriert ist oder `treffer` leer ist (CLAUDE.md: nur senden, wenn es etwas zu
    melden gibt). Ein SMTP-Fehler (Verbindung, Login, ...) wird abgefangen und geloggt statt
    weitergereicht - ein Versandfehler darf einen sonst erfolgreichen Scraper-Lauf nicht
    abbrechen (derselbe Isolations-Grundsatz wie bei einzelnen Quellen).

    `web_funde` (neue Funde der Web-Recherche) erscheinen als eigener Abschnitt; eine Mail geht
    auch dann raus, wenn nur Web-Funde neu sind.

    `smtp_client_factory` ist für Tests da (Fake-SMTP-Server statt echtem Versand)."""
    web_funde = web_funde or []
    config = config or load_smtp_config()
    if config is None:
        logger.info("SMTP nicht konfiguriert, keine Benachrichtigung gesendet")
        return False
    if not treffer and not web_funde:
        logger.info("Keine neuen Treffer, keine Benachrichtigung gesendet")
        return False

    msg = build_message(config, treffer, web_funde)
    try:
        with smtp_client_factory(config.host, config.port) as client:
            client.starttls()
            client.login(config.user, config.password)
            client.sendmail(config.user, list(config.recipients), msg.as_string())
    except Exception:
        logger.exception("Benachrichtigung konnte nicht gesendet werden")
        return False

    logger.info(
        "Benachrichtigung mit %d Treffer(n) und %d Web-Fund(en) an %s gesendet",
        len(treffer),
        len(web_funde),
        ", ".join(config.recipients),
    )
    return True
