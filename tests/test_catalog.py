import io
import json
import os
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest
from pypdf import PdfWriter

from politica_monetaria_banrep_rag.catalog import Catalog, CatalogError
from politica_monetaria_banrep_rag.download import DownloadError, PdfDownloader
from politica_monetaria_banrep_rag.source import ReportRecord


def report(month):
    return ReportRecord(
        f"ipm-2026-{month:02d}",
        f"Informe de Política Monetaria - {month}",
        "main_report",
        "es",
        f"2026-{month:02d}",
        None,
        None,
        "2026-10-16",
        None,
        f"https://www.banrep.gov.co/es/informe/{month}",
        f"https://doi.org/rh{month}",
    )


class Source:
    def __init__(self, reports):
        self.reports = reports

    def discover(self, count):
        return self.reports[:count]


@pytest.fixture
def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def make_catalog(tmp_path, source, respond):
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return Catalog(tmp_path / "estado", source, PdfDownloader(client, attempts=1)), client


def test_sync_activates_only_complete_window_and_is_idempotent(tmp_path, pdf_bytes):
    source = Source([report(7), report(4)])
    catalog, client = make_catalog(
        tmp_path, source, lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
    )
    with client:
        first = catalog.sync(2)
        pointer = catalog.root / "current.json"
        os.utime(pointer, (946684800, 946684800))
        mtime = pointer.stat().st_mtime_ns
        source.reports = [replace(r, discovered_at="2026-10-17") for r in source.reports]
        assert catalog.sync(2) == first
        assert pointer.stat().st_mtime_ns == mtime
        active_id, snapshot = catalog.current()
    assert active_id == first
    assert [entry["report"]["report_id"] for entry in snapshot["reports"]] == [
        "ipm-2026-07",
        "ipm-2026-04",
    ]
    assert len(list((catalog.root / "snapshots").glob("*.json"))) == 1


def test_correction_and_variable_window_preserve_old_snapshot(tmp_path, pdf_bytes):
    source = Source([report(7), report(4)])
    current_pdf = [pdf_bytes]
    catalog, client = make_catalog(
        tmp_path, source, lambda _: httpx.Response(200, stream=httpx.ByteStream(current_pdf[0]))
    )
    with client:
        original = catalog.sync(2)
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.add_metadata({"/Title": "Corrección"})
        output = io.BytesIO()
        writer.write(output)
        current_pdf[0] = output.getvalue()
        corrected = catalog.sync(2)
        narrower = catalog.sync(1)
    assert len({original, corrected, narrower}) == 3
    assert all(
        (catalog.root / "snapshots" / f"{sid}.json").exists() for sid in (original, corrected)
    )
    assert len(list((catalog.root / "raw").glob("*.pdf"))) == 4
    assert len(catalog.current()[1]["reports"]) == 1


def test_failed_second_download_keeps_previous_pointer(tmp_path, pdf_bytes):
    source = Source([report(7)])
    fail = [False]

    def respond(request):
        if fail[0] and str(request.url).endswith("rh4"):
            return httpx.Response(404)
        return httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))

    catalog, client = make_catalog(tmp_path, source, respond)
    with client:
        original = catalog.sync(1)
        source.reports.append(report(4))
        fail[0] = True
        with pytest.raises(DownloadError, match="HTTP 404"):
            catalog.sync(2)
    assert catalog.current()[0] == original
    assert len(list((catalog.root / "snapshots").glob("*.json"))) == 1


def test_failed_pointer_swap_leaves_previous_snapshot_active(tmp_path, pdf_bytes, monkeypatch):
    source = Source([report(7), report(4)])
    catalog, client = make_catalog(
        tmp_path, source, lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
    )
    with client:
        original = catalog.sync(1)
        real_replace = os.replace

        def fail_pointer(src, dst):
            if str(dst).endswith("current.json"):
                raise OSError("Disco sin espacio")
            return real_replace(src, dst)

        monkeypatch.setattr("politica_monetaria_banrep_rag.catalog.os.replace", fail_pointer)
        with pytest.raises(OSError, match="Disco"):
            catalog.sync(2)
    assert catalog.current()[0] == original
    assert len(list((catalog.root / "snapshots").glob("*.json"))) == 2


@pytest.mark.parametrize(
    "invalid",
    [
        [report(4), report(7)],
        [report(7), report(7)],
        [report(7), replace(report(4), document_type="presentation")],
        [
            report(7),
            replace(
                report(4),
                download_url="https://repositorio.banrep.gov.co/server/api/core/bitstreams/7a119fee-bead-43c1-9671-78e3f45ef54a/content",
            ),
        ],
        [report(7), replace(report(4), download_url="https://evil.example/ipm.pdf")],
    ],
)
def test_invalid_window_never_downloads_or_activates(tmp_path, invalid):
    def forbidden(_):
        pytest.fail("No se debe descargar una ventana inválida")

    catalog, client = make_catalog(tmp_path, Source(invalid), forbidden)
    with client, pytest.raises(CatalogError):
        catalog.sync(2)
    assert catalog.current() is None


def test_corrupt_active_snapshot_is_not_overwritten(tmp_path, pdf_bytes):
    catalog, client = make_catalog(
        tmp_path,
        Source([report(7)]),
        lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes)),
    )
    with client:
        snapshot_id = catalog.sync(1)
        path = catalog.root / "snapshots" / f"{snapshot_id}.json"
        path.write_text(json.dumps({"version": 1, "reports": []}))
        with pytest.raises(CatalogError, match="dañado"):
            catalog.sync(1)
    assert json.loads((catalog.root / "current.json").read_text())["snapshot"] == snapshot_id


def test_incomplete_discovery_does_not_replace_active_window(tmp_path, pdf_bytes):
    source = Source([report(7)])
    catalog, client = make_catalog(
        tmp_path,
        source,
        lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes)),
    )
    with client:
        original = catalog.sync(1)
        with pytest.raises(CatalogError, match="toda la ventana"):
            catalog.sync(2)
    assert catalog.current()[0] == original


def test_raw_directory_sync_failure_keeps_prior_pointer(tmp_path, pdf_bytes, monkeypatch):
    source = Source([report(7), report(4)])
    catalog, client = make_catalog(
        tmp_path, source, lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
    )
    with client:
        original = catalog.sync(1)
        from politica_monetaria_banrep_rag import catalog as module

        real_sync = module._sync_directory

        def fail_raw(path):
            if path == catalog.root / "raw":
                raise OSError("Directorio no sincronizado")
            real_sync(path)

        monkeypatch.setattr(module, "_sync_directory", fail_raw)
        with pytest.raises(OSError, match="Directorio"):
            catalog.sync(2)
    assert catalog.current()[0] == original


def test_catalog_imports_without_fcntl():
    code = "import sys; sys.modules['fcntl'] = None; import politica_monetaria_banrep_rag.cli"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_total_deadline_stops_before_activation(tmp_path, pdf_bytes):
    ticks = iter([0.0, 0.0, 5000.0])
    messages = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf_bytes))
        )
    )
    catalog = Catalog(
        tmp_path / "estado",
        Source([report(7), report(4)]),
        PdfDownloader(client, attempts=1),
        progress=messages.append,
        max_seconds=60,
        clock=lambda: next(ticks),
    )
    with client, pytest.raises(CatalogError, match="plazo total"):
        catalog.sync(2)
    assert not (catalog.root / "current.json").exists()
    assert messages[-1] == "Descargando 1/2: ipm-2026-07…"


@pytest.mark.parametrize("limit", [0, -1, float("inf"), float("nan")])
def test_invalid_total_deadline_is_rejected(tmp_path, limit):
    with pytest.raises(ValueError, match="plazo total"):
        Catalog(tmp_path, Source([]), PdfDownloader(), max_seconds=limit)
