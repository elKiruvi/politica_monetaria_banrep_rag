import io
import json

import httpx
import pytest
from pypdf import PdfWriter

from politica_monetaria_banrep_rag.cli import main
from politica_monetaria_banrep_rag.download import PdfDownloader
from politica_monetaria_banrep_rag.source import ReportRecord, SourceError


@pytest.fixture(autouse=True)
def historical_source_by_default(monkeypatch):
    """Las pruebas heredadas sustituyen BanrepSource; se fija esa fuente explícitamente."""
    monkeypatch.setenv("BANREP_SOURCE", "historico")


def test_diagnostico(capsys, monkeypatch):
    monkeypatch.setenv("BANREP_MAX_REPORTS", "no_es_entero")
    monkeypatch.setattr("sys.argv", ["banrep-rag", "diagnostico"])
    main()
    assert "las comparaciones entre informes aún no" in capsys.readouterr().out


def test_descubrir_json_with_explicit_n(capsys, monkeypatch):
    monkeypatch.setenv("BANREP_MAX_REPORTS", "no_es_entero")
    monkeypatch.setattr("sys.argv", ["banrep-rag", "descubrir", "--max-reports", "1", "--json"])

    def discover(_, maximum):
        assert maximum == 1
        return [
            ReportRecord(
                "ipm-2030-05",
                "Informe de prueba",
                "main_report",
                "es",
                "2026-07",
                None,
                None,
                "2026-10-15",
                None,
                "https://www.banrep.gov.co/es/x",
                "https://doi.org/x",
            )
        ]

    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.BanrepSource.discover", discover)
    main()
    assert json.loads(capsys.readouterr().out)[0]["report_id"] == "ipm-2030-05"


def test_descubrir_reports_source_error(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "descubrir"])

    def blocked(_, maximum):
        raise SourceError("Bloqueo del histórico.")

    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.BanrepSource.discover", blocked)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "Bloqueo" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["descubrir"], ["descubrir", "--max-reports", "xyz"]])
def test_invalid_count_is_reported_in_spanish(capsys, monkeypatch, argv):
    monkeypatch.setenv("BANREP_MAX_REPORTS", "abc")
    monkeypatch.setattr("sys.argv", ["banrep-rag", *argv])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "la cantidad de informes debe ser un número entero" in message
    assert "invalid literal" not in message


def test_sincronizar_activates_catalog_with_offline_clients(tmp_path, capsys, monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    pdf = output.getvalue()
    clients = []

    class FakeSource:
        def __init__(self):
            self.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
            clients.append(self.client)

        def discover(self, count):
            assert count == 1
            return [
                ReportRecord(
                    "ipm-2026-07",
                    "Informe de Política Monetaria - julio 2026",
                    "main_report",
                    "es",
                    "2026-07",
                    None,
                    None,
                    "2026-10-16",
                    None,
                    "https://www.banrep.gov.co/es/informe/julio",
                    "https://doi.org/rh7c",
                )
            ]

    def fake_downloader():
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=httpx.ByteStream(pdf))
            )
        )
        clients.append(client)
        return PdfDownloader(client)

    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.BanrepSource", FakeSource)
    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.PdfDownloader", fake_downloader)
    monkeypatch.setenv("BANREP_MAX_REPORTS", "1")
    monkeypatch.setenv("BANREP_DATA_DIR", str(tmp_path / "estado"))
    monkeypatch.setattr("sys.argv", ["banrep-rag", "sincronizar"])
    main()
    active = json.loads((tmp_path / "estado/current.json").read_text())
    assert active["snapshot"] in capsys.readouterr().out
    assert len(list((tmp_path / "estado/raw").glob("*.pdf"))) == 1
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("count", ["x", "0", "101"])
def test_sincronizar_rejects_invalid_count_before_network(capsys, monkeypatch, count):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "sincronizar", "--max-reports", count])
    monkeypatch.setattr(
        "politica_monetaria_banrep_rag.cli.BanrepSource",
        lambda: pytest.fail("No se debe abrir un cliente HTTP"),
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "cantidad de informes" in capsys.readouterr().err


def test_sincronizar_reports_blocked_source_without_active_catalog(tmp_path, capsys, monkeypatch):
    class BlockedSource:
        def __init__(self):
            self.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail()))

        def discover(self, count):
            raise SourceError("Bloqueo del histórico.")

    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.BanrepSource", BlockedSource)
    monkeypatch.setattr(
        "politica_monetaria_banrep_rag.cli.PdfDownloader",
        lambda: PdfDownloader(httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail()))),
    )
    monkeypatch.setattr("sys.argv", ["banrep-rag", "sincronizar", "--data-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "Bloqueo del histórico" in capsys.readouterr().err
    assert not (tmp_path / "current.json").exists()


def test_sincronizar_closes_source_when_downloader_setup_fails(tmp_path, capsys, monkeypatch):
    clients = []

    class Source:
        def __init__(self):
            self.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail()))
            clients.append(self.client)

    def broken_downloader():
        raise FileNotFoundError(2, "No such file", "/certificados/inexistente.pem")

    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.BanrepSource", Source)
    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.PdfDownloader", broken_downloader)
    monkeypatch.setattr("sys.argv", ["banrep-rag", "sincronizar", "--data-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "fallo del sistema local en /certificados/inexistente.pem" in message
    assert "No such file" not in message
    assert all(client.is_closed for client in clients)


def test_descubrir_uses_crossref_by_default(capsys, monkeypatch):
    monkeypatch.delenv("BANREP_SOURCE")
    monkeypatch.setenv("BANREP_CONTACT_EMAIL", "persona@example.org")
    created = {}

    class FakeCrossref:
        def __init__(self, contact_email=None):
            created["email"] = contact_email
            self.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail()))

        def discover(self, count):
            return [
                ReportRecord(
                    "ipm-2026-07", "Informe de Política Monetaria - julio de 2026", "main_report",
                    "es", "2026-07", "2026-08-04", "crossref:published", "2026-10-16", None,
                    "https://doi.org/10.32468/inf-pol-mont-spa.tr3-2026",
                    "https://repositorio.banrep.gov.co/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf",
                    doi="10.32468/inf-pol-mont-spa.tr3-2026", discovery_source="crossref",
                )
            ][:count]  # fmt: skip

    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.CrossrefReportSource", FakeCrossref)
    monkeypatch.setattr("sys.argv", ["banrep-rag", "descubrir", "--max-reports", "1", "--json"])
    main()
    data = json.loads(capsys.readouterr().out)
    assert data[0]["doi"] == "10.32468/inf-pol-mont-spa.tr3-2026"
    assert data[0]["discovery_source"] == "crossref"
    assert created["email"] == "persona@example.org"


def test_invalid_source_name_is_rejected_in_spanish(capsys, monkeypatch):
    monkeypatch.setenv("BANREP_SOURCE", "dspace")
    monkeypatch.setattr("sys.argv", ["banrep-rag", "descubrir"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "«crossref» o «historico»" in capsys.readouterr().err


def test_sincronizar_dry_run_prints_plan_without_downloading(tmp_path, capsys, monkeypatch):
    class Source:
        def __init__(self):
            self.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail()))

        def discover(self, count):
            return [
                ReportRecord(
                    "ipm-2026-07", "Informe de Política Monetaria - julio de 2026", "main_report",
                    "es", "2026-07", None, None, "2026-10-16", None,
                    "https://www.banrep.gov.co/es/informe/julio", "https://doi.org/rh7c",
                )
            ]  # fmt: skip

    monkeypatch.setattr("politica_monetaria_banrep_rag.cli.BanrepSource", Source)
    monkeypatch.setattr(
        "sys.argv",
        ["banrep-rag", "sincronizar", "--max-reports", "1", "--data-dir", str(tmp_path / "d"),
         "--dry-run"],
    )  # fmt: skip
    main()
    out = capsys.readouterr().out
    assert "Simulación" in out and "ipm-2026-07 — nuevo, se descargará" in out
    assert not (tmp_path / "d").exists()


@pytest.mark.parametrize("value", ["-1", "abc", "inf"])
def test_sincronizar_rejects_invalid_pause(capsys, monkeypatch, value):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "sincronizar", "--pausa", value])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_sincronizar_rejects_pause_above_limit(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "sincronizar", "--pausa", "601"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "entre 0 y 600" in capsys.readouterr().err
