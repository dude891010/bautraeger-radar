"""Gemeinsame Test-Hilfsmittel, siehe pytest-Doku zu `conftest.py`."""


class _FakeUsage:
    input_tokens = 42
    output_tokens = 7


class _FakeParsedResponse:
    def __init__(self, parsed_output):
        self.parsed_output = parsed_output
        self.usage = _FakeUsage()


class FakeAnthropicClient:
    """Ersetzt `anthropic.Anthropic` in Tests: validiert die vorgegebenen Roh-Daten wie die echte
    `messages.parse()`-Methode gegen `output_format` (wirft `pydantic.ValidationError`, wenn die
    Rohdaten nicht zum Schema passen) und liefert der Reihe nach vorgegebene Antworten."""

    def __init__(self, raw_responses):
        self._raw_responses = list(raw_responses)
        self.calls: list[dict] = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        raw = self._raw_responses.pop(0)
        validated = kwargs["output_format"].model_validate(raw)
        return _FakeParsedResponse(validated)


def make_pdf(*page_texts: str | None) -> bytes:
    """Gültiges, minimales Mehrseiten-PDF mit Klartext je Seite (oder leerem Content-Stream für
    `None`, simuliert eine Seite ohne Textlayer). Keine externe PDF-Bibliothek als Testabhängigkeit
    nötig - die Byte-Offsets für xref werden hier direkt berechnet."""
    objects: dict[int, bytes] = {}
    n_pages = len(page_texts)
    page_obj_nums = [3 + i for i in range(n_pages)]
    content_obj_nums = [3 + n_pages + i for i in range(n_pages)]
    font_obj_num = 3 + 2 * n_pages

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("ascii")

    for page_num, content_num, text in zip(page_obj_nums, content_obj_nums, page_texts, strict=True):
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> /Contents {content_num} 0 R >>"
        ).encode("ascii")
        content = f"BT /F1 12 Tf 10 700 Td ({text}) Tj ET".encode("latin-1") if text else b""
        objects[content_num] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)

    objects[font_obj_num] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    buf = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(buf)
        buf += f"{num} 0 obj\n".encode("ascii") + objects[num] + b"\nendobj\n"

    xref_offset = len(buf)
    total = len(objects) + 1
    buf += f"xref\n0 {total}\n".encode("ascii")
    buf += b"0000000000 65535 f \n"
    for num in sorted(objects):
        buf += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    buf += f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")
    return bytes(buf)
