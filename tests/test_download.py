import gzip
import hashlib
import io
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pypdf import PdfWriter

from politica_monetaria_banrep_rag.download import (
    DownloadBlockedError,
    DownloadError,
    PdfDownloader,
    _retry_after,
)
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
                headers={
                    "location": "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf"
                },
            )
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(pdf_bytes)
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = PdfDownloader(client).download(report, tmp_path)
    assert result.sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert result.bytes_count == len(pdf_bytes)
    assert result.pages_count == 1
    assert result.path.read_bytes() == pdf_bytes
    assert (
        result.final_url
        == "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf"
    )
    assert len(visited) == 2
    assert list(tmp_path.glob(".descarga-*")) == []


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
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError):
            PdfDownloader(client).download(report, tmp_path)
    assert len(visited) == 1
    assert list(tmp_path.glob("*.pdf")) == []
    assert list(tmp_path.glob(".descarga-*")) == []


def test_size_limit_cleans_temporary_file(tmp_path, report, pdf_bytes):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    ) as client:
        with pytest.raises(DownloadError, match="límite"):
            PdfDownloader(client, max_bytes=50).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_bad_report_id_cannot_traverse_directories(tmp_path, report):
    with pytest.raises(DownloadError, match="identificador"):
        PdfDownloader().download(replace(report, report_id="../archivo"), tmp_path)
    assert not list(tmp_path.iterdir())


def test_gzip_transport_uses_uncompressed_pdf_size(tmp_path, report, pdf_bytes):
    compressed = gzip.compress(pdf_bytes)
    headers = {
        "content-type": "application/pdf",
        "content-encoding": "gzip",
        "content-length": str(len(compressed)),
    }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=headers, stream=httpx.ByteStream(compressed))
        )
    ) as client:
        result = PdfDownloader(client).download(report, tmp_path)
    assert result.path.read_bytes() == pdf_bytes
    assert result.bytes_count == len(pdf_bytes)


def test_gzip_bomb_is_limited_before_expansion(tmp_path, report):
    compressed = gzip.compress(b"%PDF-" + b"x" * 5_000_000)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(compressed)
            )
        )
    ) as client:
        with pytest.raises(DownloadError, match="límite"):
            PdfDownloader(client, max_bytes=10_000).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_gzip_trailing_data_is_rejected(tmp_path, report, pdf_bytes):
    compressed = gzip.compress(pdf_bytes) + b"<html>contenido ajeno</html>"
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(compressed)
            )
        )
    ) as client:
        with pytest.raises(DownloadError, match="datos sobrantes"):
            PdfDownloader(client).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


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
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(output.getvalue()))
        )
    ) as client:
        with pytest.raises(DownloadError, match="páginas"):
            PdfDownloader(client, max_pages=1).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"auth": ("usuario", "secreto")},
        {"headers": {"Cookie": "sesion=privada"}},
        {"headers": {"X-Api-Key": "secreto"}},
        {"params": {"api_key": "secreto"}},
    ],
)
def test_rejects_authenticated_clients(kwargs):
    with httpx.Client(**kwargs) as client:
        with pytest.raises(ValueError, match="credenciales"):
            PdfDownloader(client)


def test_rejects_credentials_added_after_initialization(report, tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("No debe haber solicitud"))
    ) as client:
        downloader = PdfDownloader(client)
        client.headers["Authorization"] = "Bearer secreto"
        with pytest.raises(ValueError, match="credenciales"):
            downloader.download(report, tmp_path)


def test_cookie_set_by_doi_is_not_sent_to_repository(tmp_path, report, pdf_bytes):
    def respond(request):
        if request.url.host == "doi.org":
            return httpx.Response(
                302,
                headers={
                    "location": "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/11411/informe.pdf",
                    "set-cookie": "sesion=no-reenviar; Path=/",
                },
            )
        assert "cookie" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert PdfDownloader(client).download(report, tmp_path).pages_count == 1


def test_total_deadline_aborts_stream_and_cleans_file(tmp_path, report, pdf_bytes, monkeypatch):
    clock = 0

    def monotonic():
        nonlocal clock
        clock += 1
        return clock

    monkeypatch.setattr("politica_monetaria_banrep_rag.download.time.monotonic", monotonic)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    ) as client:
        with pytest.raises(DownloadError, match="tiempo máximo"):
            PdfDownloader(client, max_seconds=2).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_repeated_bytes_keep_mtime_and_correction_keeps_old_pdf(tmp_path, report, pdf_bytes):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "Corrección sintética"})
    output = io.BytesIO()
    writer.write(output)
    newer = output.getvalue()
    delivered = [pdf_bytes, pdf_bytes, newer]

    def respond(_):
        return httpx.Response(200, stream=httpx.ByteStream(delivered.pop(0)))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        downloader = PdfDownloader(client)
        first = downloader.download(report, tmp_path)
        os.utime(first.path, (946684800, 946684800))
        initial_mtime = first.path.stat().st_mtime_ns
        assert downloader.download(report, tmp_path).path == first.path
        assert first.path.stat().st_mtime_ns == initial_mtime
        updated = downloader.download(report, tmp_path)
    assert updated.sha256 != first.sha256
    assert first.path.read_bytes() == pdf_bytes
    assert updated.path.read_bytes() == newer
    assert len(list(tmp_path.glob("*.pdf"))) == 2


@pytest.mark.parametrize("status", [408, 429, 503])
def test_transient_status_retries_with_retry_after(
    tmp_path, report, pdf_bytes, monkeypatch, status
):
    calls = 0
    delays = []
    monkeypatch.setattr("politica_monetaria_banrep_rag.download.time.sleep", delays.append)

    def respond(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, headers={"retry-after": "0"})
        return httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert PdfDownloader(client).download(report, tmp_path).pages_count == 1
    assert calls == 2
    assert delays == [0]


def test_permanent_http_error_does_not_retry(tmp_path, report):
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError, match="HTTP 404"):
            PdfDownloader(client).download(report, tmp_path)
    assert calls == 1


def test_retries_are_bounded_and_do_not_leave_partial_files(tmp_path, report, monkeypatch):
    calls = 0
    monkeypatch.setattr("politica_monetaria_banrep_rag.download.time.sleep", lambda _: None)

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"retry-after": "0"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError, match="reintentos"):
            PdfDownloader(client, attempts=3).download(report, tmp_path)
    assert calls == 3
    assert list(tmp_path.iterdir()) == []


def test_existing_symlink_is_rejected(tmp_path, report, pdf_bytes):
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    outside = tmp_path.parent / "fuera.pdf"
    outside.write_bytes(pdf_bytes)
    (tmp_path / f"{report.report_id}-{digest}.pdf").symlink_to(outside)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    ) as client:
        with pytest.raises(DownloadError, match="destino"):
            PdfDownloader(client).download(report, tmp_path)
    assert outside.read_bytes() == pdf_bytes


def test_retry_after_parses_http_date_and_ignores_invalid_values():
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 0 < _retry_after(later) <= 30
    assert _retry_after("120") == 120.0
    assert _retry_after("NaN") is None
    assert _retry_after("fecha imposible") is None


def test_invalid_download_limits_are_rejected():
    with pytest.raises(ValueError, match="límites"):
        PdfDownloader(max_seconds=float("nan"))
    with pytest.raises(ValueError, match="límites"):
        PdfDownloader(attempts=6)
    with pytest.raises(ValueError, match="límites"):
        PdfDownloader(attempts=1.5)


def test_retry_after_longer_than_deadline_stops_without_early_retry(tmp_path, report):
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "120"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError, match="tiempo máximo"):
            PdfDownloader(client, max_seconds=30).download(report, tmp_path)
    assert calls == 1


def test_symlink_inserted_before_open_is_rejected(tmp_path, report, pdf_bytes, monkeypatch):
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    target = tmp_path / f"{report.report_id}-{digest}.pdf"
    target.write_bytes(pdf_bytes)
    outside = tmp_path.parent / "documento-externo.pdf"
    outside.write_bytes(pdf_bytes)
    original_open = os.open

    def replaced_open(path, flags, *args, **kwargs):
        if str(path) == str(target):
            target.unlink()
            target.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("politica_monetaria_banrep_rag.download.os.open", replaced_open)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    ) as client:
        with pytest.raises(DownloadError, match="seguridad"):
            PdfDownloader(client).download(report, tmp_path)
    assert outside.read_bytes() == pdf_bytes


def test_without_no_follow_replaces_instead_of_reading_existing_file(
    tmp_path, report, pdf_bytes, monkeypatch
):
    target = tmp_path / f"{report.report_id}-{hashlib.sha256(pdf_bytes).hexdigest()}.pdf"
    target.write_bytes(b"x" * len(pdf_bytes))
    monkeypatch.delattr("politica_monetaria_banrep_rag.download.os.O_NOFOLLOW", raising=False)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    ) as client:
        assert PdfDownloader(client).download(report, tmp_path).path == target
    assert target.read_bytes() == pdf_bytes


def test_injected_client_without_timeout_receives_bounded_request_timeout(
    tmp_path, report, pdf_bytes
):
    observed = []

    def respond(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))

    with httpx.Client(transport=httpx.MockTransport(respond), timeout=None) as client:
        assert PdfDownloader(client, max_seconds=3).download(report, tmp_path).pages_count == 1
    assert len(observed) == 1
    assert 0 < observed[0]["read"] <= 2
    assert 0 < observed[0]["connect"] <= 3


def test_fifo_inserted_before_open_cannot_block_download(tmp_path, report, pdf_bytes, monkeypatch):
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("La plataforma no proporciona FIFO no bloqueante")
    target = tmp_path / f"{report.report_id}-{hashlib.sha256(pdf_bytes).hexdigest()}.pdf"
    target.write_bytes(pdf_bytes)
    original_open = os.open

    def insert_fifo(path, flags, *args, **kwargs):
        if str(path) == str(target):
            target.unlink()
            os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("politica_monetaria_banrep_rag.download.os.open", insert_fifo)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    ) as client:
        with pytest.raises(DownloadError, match="regular"):
            PdfDownloader(client, max_seconds=1).download(report, tmp_path)


def test_hashing_existing_file_cannot_succeed_after_deadline(
    tmp_path, report, pdf_bytes, monkeypatch
):
    target = tmp_path / f"{report.report_id}-{hashlib.sha256(pdf_bytes).hexdigest()}.pdf"
    target.write_bytes(pdf_bytes)
    clock = [0.0]
    monkeypatch.setattr("politica_monetaria_banrep_rag.download.time.monotonic", lambda: clock[0])
    original_digest = hashlib.file_digest

    def slow_digest(*args, **kwargs):
        clock[0] = 2.0
        return original_digest(*args, **kwargs)

    monkeypatch.setattr("politica_monetaria_banrep_rag.download.hashlib.file_digest", slow_digest)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    ) as client:
        with pytest.raises(DownloadError, match="tiempo máximo"):
            PdfDownloader(client, max_seconds=1).download(report, tmp_path)
    assert target.read_bytes() == pdf_bytes


def test_doi_handle_bitstream_content_chain_is_followed(tmp_path, report, pdf_bytes):
    uuid = "7a119fee-bead-43c1-9671-78e3f45ef54a"
    repo = "https://repositorio.banrep.gov.co"
    chain = {
        "https://doi.org/rh7c": "https://doi.org/10.32468/INF-POL-MONT-SPA.TR3-2026",
        "https://doi.org/10.32468/INF-POL-MONT-SPA.TR3-2026": (
            f"{repo}/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf"
        ),
        f"{repo}/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf": (
            f"/bitstreams/{uuid}/download"
        ),
        f"{repo}/bitstreams/{uuid}/download": f"/server/api/core/bitstreams/{uuid}/content",
    }
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if str(request.url) in chain:
            return httpx.Response(302, headers={"location": chain[str(request.url)]})
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(pdf_bytes)
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = PdfDownloader(client).download(report, tmp_path)
    assert result.final_url == f"{repo}/server/api/core/bitstreams/{uuid}/content"
    assert len(visited) == 5
    assert result.path.read_bytes() == pdf_bytes


@pytest.mark.parametrize(
    "location",
    [
        "https://validate.perfdrive.com/?ssa=1&ssc=https%3A%2F%2Frepositorio.banrep.gov.co",
        "https://otro.perfdrive.com/desafio",
    ],
)
def test_antibot_redirect_is_reported_as_blocked(tmp_path, report, location):
    def respond(_):
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadBlockedError, match="antibot"):
            PdfDownloader(client).download(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_html_instead_of_pdf_is_reported(tmp_path, report):
    def respond(_):
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html></html>")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError, match="HTML en lugar del PDF"):
            PdfDownloader(client).download(report, tmp_path)


def test_dspace_content_url_cannot_be_the_starting_point(tmp_path, report):
    url = "https://repositorio.banrep.gov.co/server/api/core/bitstreams/7a119fee-bead-43c1-9671-78e3f45ef54a/content"
    forbidden = httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Sin solicitudes"))
    )
    with forbidden, pytest.raises(DownloadError, match="no es oficial"):
        PdfDownloader(forbidden).download(replace(report, download_url=url), tmp_path)


@pytest.mark.parametrize(
    "location",
    [
        "https://repositorio.banrep.gov.co/server/api/core/bitstreams/7a119fee-bead-43c1-9671-78e3f45ef54a/content",
        "https://repositorio.banrep.gov.co/bitstreams/7a119fee-bead-43c1-9671-78e3f45ef54a/download",
        "https://repositorio.banrep.gov.co/x/../bitstream/handle/20.500.12134/1/IPM.pdf",
    ],
)
def test_doi_cannot_redirect_straight_to_dspace_bitstreams(tmp_path, report, location):
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if request.url.host == "doi.org":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError, match="no autorizado"):
            PdfDownloader(client).download(report, tmp_path)
    assert visited == ["https://doi.org/rh7c"]


@pytest.mark.parametrize(
    "location",
    ["https://repositorio.banrep.gov.co/a\tb.pdf", "https://[::1/x.pdf", "https://doi.org:99999/x"],
)
def test_malformed_location_is_permanent_not_retried(tmp_path, report, location):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError, match="redirección mal formada|no autorizado"):
            PdfDownloader(client).download(report, tmp_path)
    assert len(calls) == 1


def test_challenge_host_with_trailing_dot_is_blocked(tmp_path, report):
    def respond(_):
        return httpx.Response(302, headers={"location": "https://validate.perfdrive.com./x"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadBlockedError):
            PdfDownloader(client).download(report, tmp_path)
