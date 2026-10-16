import gzip
import hashlib
import io
from dataclasses import replace

import httpx
import pytest
from pypdf import PdfWriter

from politica_monetaria_banrep_rag.download import DownloadError, PdfDownloader
from politica_monetaria_banrep_rag.source import ReportRecord


@pytest.fixture
def report():
    return ReportRecord(
        "ipm-2026-07",
        "Informe de prueba",
        "main_report",
        "es",
        "2026-07",
        None,
        None,
        "2026-10-15",
        None,
        "https://www.banrep.gov.co/es/informe/julio",
        "https://doi.org/rh7c",
    )


@pytest.fixture
def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_redirects_to_official_repository_and_saves_atomically(tmp_path, report, pdf_bytes):
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if request.url.host == "doi.org":
            return httpx.Response(
                302,
                headers={"location": "https://repositorio.banrep.gov.co/bitstream/ipm-julio.pdf"},
            )
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf_bytes)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = PdfDownloader(client).download(report, tmp_path)
    assert result.sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert result.bytes_count == len(pdf_bytes)
    assert result.pages_count == 1
    assert result.path.read_bytes() == pdf_bytes
    assert result.final_url == "https://repositorio.banrep.gov.co/bitstream/ipm-julio.pdf"
    assert len(visited) == 2
    assert list(tmp_path.glob(".descarga-*")) == []


def test_same_bytes_are_idempotent_and_correction_preserves_first_copy(tmp_path, report, pdf_bytes):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "Corrección sintética"})
    output = io.BytesIO()
    writer.write(output)
    newer = output.getvalue()
    delivered = [pdf_bytes, pdf_bytes, newer]

    def respond(_):
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=delivered.pop(0)
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        downloader = PdfDownloader(client)
        first = downloader.download(report, tmp_path)
        initial_mtime = first.path.stat().st_mtime_ns
        assert downloader.download(report, tmp_path).path == first.path
        assert first.path.stat().st_mtime_ns == initial_mtime
        updated = downloader.download(report, tmp_path)
        assert updated.sha256 != first.sha256
        assert first.path.read_bytes() == pdf_bytes
        assert updated.path.read_bytes() == newer


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (302, {"location": "http://127.0.0.1/evil.pdf"}, b""),
        (302, {"location": "https://validate.perfdrive.com/evil.pdf"}, b""),
        (200, {"content-type": "text/html"}, b"<html>Bloqueado</html>"),
        (200, {"content-type": "application/pdf"}, b"%PDF-1.7\ntruncado"),
        (200, {"content-type": "application/pdf", "content-length": "99999"}, b"%PDF-1.7\n"),
    ],
)
def test_rejects_unsafe_or_invalid_responses(tmp_path, report, status, headers, body):
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(status, headers=headers, content=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError):
            PdfDownloader(client, attempts=1).download(report, tmp_path)
    assert len(visited) == 1
    assert list(tmp_path.glob("*.pdf")) == []
    assert list(tmp_path.glob(".descarga-*")) == []


def test_size_limit_cleans_temporary_file(tmp_path, report, pdf_bytes):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=pdf_bytes))
    ) as client:
        with pytest.raises(DownloadError, match="límite"):
            PdfDownloader(client, max_bytes=50).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_bad_report_id_cannot_traverse_directories(tmp_path, report):
    with pytest.raises(DownloadError, match="identificador"):
        PdfDownloader().download(replace(report, report_id="../archivo"), tmp_path)
    assert not list(tmp_path.iterdir())


def test_retry_after_then_success(tmp_path, report, pdf_bytes, monkeypatch):
    calls = 0
    delays = []
    monkeypatch.setattr("politica_monetaria_banrep_rag.download.time.sleep", delays.append)

    def respond(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, content=pdf_bytes)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert PdfDownloader(client).download(report, tmp_path).pages_count == 1
    assert calls == 2
    assert delays == [0]


def test_gzip_transport_uses_uncompressed_pdf_size(tmp_path, report, pdf_bytes):
    compressed = gzip.compress(pdf_bytes)
    headers = {
        "content-type": "application/pdf",
        "content-encoding": "gzip",
        "content-length": str(len(compressed)),
    }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=headers, content=compressed)
        )
    ) as client:
        result = PdfDownloader(client).download(report, tmp_path)
    assert result.path.read_bytes() == pdf_bytes
    assert result.bytes_count == len(pdf_bytes)


def test_interrupted_stream_removes_partial_file(tmp_path, report):
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-1.7\n"
            raise httpx.ReadError("interrupción sintética")

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenStream()))
    ) as client:
        with pytest.raises(DownloadError, match="reintentos"):
            PdfDownloader(client, attempts=1).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_page_limit_rejects_valid_pdf_without_saving(tmp_path, report):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=output.getvalue()))
    ) as client:
        with pytest.raises(DownloadError, match="páginas"):
            PdfDownloader(client, max_pages=1).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []
