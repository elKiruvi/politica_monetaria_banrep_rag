"""PDF sintéticos con texto extraíble, escritos a mano para no depender de otra librería."""


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(
    pages: list[list[str]], title: str | None = None, raw_content: bytes | None = None
) -> bytes:
    """Cada página es una lista de líneas; usa Helvetica con WinAnsiEncoding (admite tildes).

    ``raw_content`` reemplaza el contenido de la primera página (para PDF hostiles).
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # se completa al final
    pages_id = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for index, lines in enumerate(pages):
        content = (
            b"BT /F1 11 Tf 14 TL 56 780 Td "
            + b" ".join(b"(" + _escape(line) + b") Tj T*" for line in lines)
            + b" ET"
        )
        if index == 0 and raw_content is not None:
            content = raw_content
        stream = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_id, font, stream)
            )
        )
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    info = add(b"<< /Title (" + _escape(title) + b") >>") if title else None
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if info:
        trailer += b" /Info %d 0 R" % info
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


_TO_UNICODE = b"""/CIDInit /ProcSet findresource begin 12 dict begin begincmap
/CMapName /Prueba-UCS def /CMapType 2 def
1 begincodespacerange <00> <FF> endcodespacerange
2 beginbfchar <B0> <0066> <B1> <0066> endbfchar
endcmap CMapName currentdict /CIDResource defineresource pop end end"""


def make_layout_pdf(pages: list[list[tuple]], width: int = 612, height: int = 842) -> bytes:
    """Texto posicionado: cada elemento es ``(x, y, tamaño, texto, fuente)``.

    Fuentes: ``"R"`` Helvetica, ``"B"`` Helvetica-Bold y ``"L"`` una fuente cuyos códigos
    0xB0/0xB1 son ligaduras «fi»/«fl» que su ToUnicode declara como «f» (PDF defectuoso real).
    El texto puede ser ``str`` (cp1252) o ``bytes``.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")
    pages_id = add(b"")
    regular = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    cmap = add(b"<< /Length %d >>\nstream\n" % len(_TO_UNICODE) + _TO_UNICODE + b"\nendstream")
    widths = [500] * 224
    widths[ord("f") - 32] = 332
    widths[0xB0 - 32] = 572
    widths[0xB1 - 32] = 590
    widths[0] = 250  # espacio
    ligature = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /AAAAAA+PruebaSans /Encoding /WinAnsiEncoding "
        b"/FirstChar 32 /LastChar 255 /Widths [" + b" ".join(b"%d" % w for w in widths) + b"] "
        b"/ToUnicode %d 0 R >>" % cmap
    )
    fonts = {"R": b"/F1", "B": b"/F2", "L": b"/F3"}
    kids = []
    for items in pages:
        ops = []
        for x, y, size, text, font in items:
            raw = text if isinstance(text, bytes) else _escape(text)
            if isinstance(text, bytes):
                raw = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
            ops.append(b"BT %s %s Tf %s %s Td (" % (fonts[font], str(size).encode(),
                       str(x).encode(), str(y).encode()) + raw + b") Tj ET")  # fmt: skip
        content = b"\n".join(ops)
        stream = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
                b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R /F3 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_id, width, height, regular, bold, ligature, stream)
            )
        )
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        xref,
    )
    return bytes(out)
