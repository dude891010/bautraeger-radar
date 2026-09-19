"""Tests für radar/notifier.py: SMTP-Konfiguration aus der Umgebung, E-Mail-Aufbau und Versand.
Kein echter SMTP-Zugriff - `smtp_client_factory` wird durch ein Fake-Objekt ersetzt (siehe
CLAUDE.md: "Netzwerk ... hinter dünnen Schnittstellen, damit Tests sie ersetzen können")."""

import email

from radar.notifier import NeuerTreffer, SMTPConfig, build_message, load_smtp_config, send_notification


class FakeSMTP:
    """Ersetzt `smtplib.SMTP`: zeichnet Aufrufe auf statt eine echte Verbindung zu öffnen."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.calls: list = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, from_addr, to_addrs, msg):
        self.calls.append(("sendmail", from_addr, to_addrs, msg))


class BoomSMTP:
    """Simuliert einen fehlschlagenden SMTP-Verbindungsaufbau (falsches Passwort, Server nicht
    erreichbar, ...)."""

    def __init__(self, host, port):
        raise ConnectionRefusedError("SMTP-Server nicht erreichbar")


def _config(**overrides):
    defaults = dict(
        host="smtp.example.org",
        port=587,
        user="bot@example.org",
        password="secret",
        recipients=("vertrieb@example.org",),
    )
    defaults.update(overrides)
    return SMTPConfig(**defaults)


def _treffer(**overrides):
    defaults = dict(kommune="Norderstedt", titel="Wohnpark Ochsenzoll", we_gesamt=18, url="https://example.org/x")
    defaults.update(overrides)
    return NeuerTreffer(**defaults)


# -- load_smtp_config -------------------------------------------------------------------------------


def test_load_smtp_config_returns_none_when_variable_missing():
    env = {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_USER": "bot@example.org",
        "SMTP_PASSWORD": "secret",
        # NOTIFICATION_RECIPIENT fehlt
    }
    assert load_smtp_config(env) is None


def test_load_smtp_config_returns_none_when_all_missing():
    assert load_smtp_config({}) is None


def test_load_smtp_config_defaults_port_to_587():
    env = {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_USER": "bot@example.org",
        "SMTP_PASSWORD": "secret",
        "NOTIFICATION_RECIPIENT": "vertrieb@example.org",
    }
    config = load_smtp_config(env)
    assert config is not None
    assert config.port == 587


def test_load_smtp_config_treats_blank_port_like_missing():
    """SMTP_PORT ist im .env.example mit '587' vorbelegt, kann aber versehentlich auf eine leere
    Zeile ('SMTP_PORT=') geleert werden - das soll wie ein fehlender Wert den Default 587 ergeben,
    nicht die Benachrichtigung stillschweigend deaktivieren."""
    env = {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_PORT": "",
        "SMTP_USER": "bot@example.org",
        "SMTP_PASSWORD": "secret",
        "NOTIFICATION_RECIPIENT": "vertrieb@example.org",
    }
    config = load_smtp_config(env)
    assert config is not None
    assert config.port == 587


def test_load_smtp_config_reads_custom_port():
    env = {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_PORT": "465",
        "SMTP_USER": "bot@example.org",
        "SMTP_PASSWORD": "secret",
        "NOTIFICATION_RECIPIENT": "vertrieb@example.org",
    }
    assert load_smtp_config(env).port == 465


def test_load_smtp_config_returns_none_for_non_numeric_port():
    env = {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_PORT": "nicht-numerisch",
        "SMTP_USER": "bot@example.org",
        "SMTP_PASSWORD": "secret",
        "NOTIFICATION_RECIPIENT": "vertrieb@example.org",
    }
    assert load_smtp_config(env) is None


def test_load_smtp_config_splits_multiple_comma_separated_recipients():
    env = {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_USER": "bot@example.org",
        "SMTP_PASSWORD": "secret",
        "NOTIFICATION_RECIPIENT": "vertrieb@example.org, it@example.org,geschaeftsfuehrung@example.org",
    }
    config = load_smtp_config(env)
    assert config is not None
    # Leerzeichen um die Kommas werden entfernt, Reihenfolge bleibt erhalten
    assert config.recipients == ("vertrieb@example.org", "it@example.org", "geschaeftsfuehrung@example.org")


def test_load_smtp_config_returns_none_when_recipient_is_only_commas():
    env = {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_USER": "bot@example.org",
        "SMTP_PASSWORD": "secret",
        "NOTIFICATION_RECIPIENT": " , , ",
    }
    assert load_smtp_config(env) is None


def test_load_smtp_config_reads_from_os_environ_by_default(monkeypatch):
    for var in ("SMTP_SERVER", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "NOTIFICATION_RECIPIENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.org")
    monkeypatch.setenv("SMTP_USER", "bot@example.org")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("NOTIFICATION_RECIPIENT", "vertrieb@example.org")

    config = load_smtp_config()

    assert config is not None
    assert config.host == "smtp.example.org"


# -- build_message ------------------------------------------------------------------------------


def test_build_message_singular_subject_for_one_treffer():
    msg = build_message(_config(), [_treffer()])
    assert msg["Subject"] == "Bauträger-Radar: 1 neues Vorhaben"


def test_build_message_plural_subject_for_multiple_treffer():
    msg = build_message(_config(), [_treffer(), _treffer(kommune="Bergedorf")])
    assert msg["Subject"] == "Bauträger-Radar: 2 neue Vorhaben"


def _decoded_parts(msg):
    """`MIMEText` kodiert UTF-8-Inhalt base64 - für Assertions erst dekodieren."""
    return {
        part.get_content_type(): part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
        for part in msg.walk()
        if part.get_content_type().startswith("text/")
    }


def test_build_message_contains_treffer_data_in_both_parts():
    msg = build_message(_config(), [_treffer(titel="Wohnpark Ochsenzoll", we_gesamt=18)])
    parts = _decoded_parts(msg)
    assert "Wohnpark Ochsenzoll" in parts["text/plain"]
    assert "18" in parts["text/plain"]
    assert "Wohnpark Ochsenzoll" in parts["text/html"]
    assert "Norderstedt" in parts["text/html"]


def test_build_message_escapes_html_special_characters():
    msg = build_message(_config(), [_treffer(titel="<script>alert(1)</script>")])
    html_part = _decoded_parts(msg)["text/html"]
    assert "<script>" not in html_part
    assert "&lt;script&gt;" in html_part


def test_build_message_shows_dash_when_no_url():
    msg = build_message(_config(), [_treffer(url=None)])
    html_part = _decoded_parts(msg)["text/html"]
    assert "<a href=" not in html_part


def test_build_message_to_header_joins_multiple_recipients():
    config = _config(recipients=("vertrieb@example.org", "it@example.org"))
    msg = build_message(config, [_treffer()])
    assert msg["To"] == "vertrieb@example.org, it@example.org"


# -- send_notification ---------------------------------------------------------------------------


def test_send_notification_returns_false_when_not_configured():
    created = []
    result = send_notification([_treffer()], config=None, smtp_client_factory=lambda h, p: created.append((h, p)))
    assert result is False
    assert created == []


def test_send_notification_returns_false_when_no_treffer():
    created = []
    result = send_notification([], config=_config(), smtp_client_factory=lambda h, p: created.append((h, p)))
    assert result is False
    assert created == []


def test_send_notification_sends_via_factory():
    created: list[FakeSMTP] = []

    def factory(host, port):
        smtp = FakeSMTP(host, port)
        created.append(smtp)
        return smtp

    config = _config()
    result = send_notification([_treffer()], config=config, smtp_client_factory=factory)

    assert result is True
    assert len(created) == 1
    smtp = created[0]
    assert (smtp.host, smtp.port) == (config.host, config.port)
    assert ("starttls",) in smtp.calls
    assert ("login", config.user, config.password) in smtp.calls
    sendmail_call = next(c for c in smtp.calls if c[0] == "sendmail")
    assert sendmail_call[1] == config.user
    assert sendmail_call[2] == list(config.recipients)
    versendete_nachricht = email.message_from_string(sendmail_call[3])
    assert "Wohnpark Ochsenzoll" in _decoded_parts(versendete_nachricht)["text/plain"]


def test_send_notification_catches_smtp_errors_and_returns_false():
    result = send_notification([_treffer()], config=_config(), smtp_client_factory=BoomSMTP)
    assert result is False


def test_send_notification_sends_to_each_recipient_individually():
    """sendmail() erwartet eine Liste einzelner Adressen als Envelope-Empfänger - eine einzelne,
    kommagetrennte Zeichenkette als ein Listenelement würde beim Versand fehlschlagen."""
    created: list[FakeSMTP] = []

    def factory(host, port):
        smtp = FakeSMTP(host, port)
        created.append(smtp)
        return smtp

    config = _config(recipients=("vertrieb@example.org", "it@example.org"))
    send_notification([_treffer()], config=config, smtp_client_factory=factory)

    sendmail_call = next(c for c in created[0].calls if c[0] == "sendmail")
    assert sendmail_call[2] == ["vertrieb@example.org", "it@example.org"]
