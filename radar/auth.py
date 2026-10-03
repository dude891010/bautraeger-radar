"""Passwortschutz für das Streamlit-Dashboard.

Streamlit selbst hat keine Anmeldung. Ohne diesen Schutz kann jeder, der die Adresse kennt (z. B.
eine öffentliche ngrok-URL), Projektdaten lesen und ändern und kostenpflichtige Läufe starten.

Konfiguration ausschließlich über `.env`:
  DASHBOARD_PASSWORD  Pflicht, mindestens 12 Zeichen.
  DASHBOARD_AUTH      Nur `extern` setzen, wenn die Firmen-IT den Zugang bereits vor der App
                      absichert (Reverse Proxy mit SSO). Dann entfällt der Passwortdialog.

Reine Logik ohne `st.*`-Aufrufe, damit sie ohne laufende Streamlit-Session testbar ist
(`tests/test_auth.py`); die Oberfläche dazu steht in `app.py` (`_login_gate`).
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections import deque
from collections.abc import Callable

MIN_PASSWORT_LAENGE = 12
MAX_FEHLVERSUCHE = 10            # innerhalb des Zeitfensters, über alle Browser-Sitzungen hinweg
ZEITFENSTER_SEKUNDEN = 15 * 60
SPERRZEIT_SEKUNDEN = 15 * 60


class AuthKonfigurationsFehler(RuntimeError):
    """Passwortschutz ist nicht (sicher) eingerichtet - das Dashboard darf dann nicht starten."""


def auth_extern() -> bool:
    return os.environ.get("DASHBOARD_AUTH", "").strip().lower() == "extern"


def dashboard_passwort() -> str:
    """Konfiguriertes Passwort, oder `AuthKonfigurationsFehler`, wenn es fehlt oder zu kurz ist.
    Bewusst kein Betrieb ohne Passwort: ein vergessener Eintrag soll das Dashboard sperren, nicht
    stillschweigend öffnen."""
    passwort = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not passwort:
        raise AuthKonfigurationsFehler(
            "DASHBOARD_PASSWORD ist nicht gesetzt. Bitte in .env ein Passwort mit mindestens "
            f"{MIN_PASSWORT_LAENGE} Zeichen eintragen (siehe .env.example) und das Dashboard neu starten."
        )
    if len(passwort) < MIN_PASSWORT_LAENGE:
        raise AuthKonfigurationsFehler(
            f"DASHBOARD_PASSWORD ist zu kurz (mindestens {MIN_PASSWORT_LAENGE} Zeichen)."
        )
    return passwort


def passwort_korrekt(eingabe: str, erwartet: str) -> bool:
    """Vergleich in konstanter Zeit (`hmac.compare_digest`), damit die Antwortzeit nichts über
    die Anzahl richtiger Anfangszeichen verrät."""
    return hmac.compare_digest(eingabe.encode("utf-8"), erwartet.encode("utf-8"))


class LoginSperre:
    """Bremst das Durchprobieren von Passwörtern: nach `MAX_FEHLVERSUCHE` Fehlversuchen innerhalb
    des Zeitfensters sind Anmeldungen für `SPERRZEIT_SEKUNDEN` gesperrt. Zählt prozessweit (nicht
    pro Browser-Sitzung), sonst könnte ein Angreifer einfach eine neue Sitzung öffnen."""

    def __init__(self, uhr: Callable[[], float] = time.monotonic):
        self._uhr = uhr
        self._fehlversuche: deque[float] = deque()
        self._gesperrt_bis = 0.0
        self._lock = threading.Lock()

    def restsperre_sekunden(self) -> int:
        with self._lock:
            return max(0, int(self._gesperrt_bis - self._uhr()))

    def fehlversuch(self) -> None:
        with self._lock:
            jetzt = self._uhr()
            self._fehlversuche.append(jetzt)
            while self._fehlversuche and jetzt - self._fehlversuche[0] > ZEITFENSTER_SEKUNDEN:
                self._fehlversuche.popleft()
            if len(self._fehlversuche) >= MAX_FEHLVERSUCHE:
                self._gesperrt_bis = jetzt + SPERRZEIT_SEKUNDEN
                self._fehlversuche.clear()


# Eine Instanz pro Streamlit-Serverprozess: Module werden nur einmal importiert, `app.py` selbst
# dagegen bei jeder Interaktion neu ausgeführt - deshalb lebt der Zähler hier und nicht dort.
LOGIN_SPERRE = LoginSperre()
