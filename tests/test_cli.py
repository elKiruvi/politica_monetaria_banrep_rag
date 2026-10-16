import json

import pytest

from politica_monetaria_banrep_rag.cli import main
from politica_monetaria_banrep_rag.source import ReportRecord, SourceError


def test_diagnostico(capsys, monkeypatch):
    monkeypatch.setenv("BANREP_MAX_REPORTS", "no_es_entero")
    monkeypatch.setattr("sys.argv", ["banrep-rag", "diagnostico"])
    main()
    assert "aún no están disponibles" in capsys.readouterr().out


def test_descubrir_json_with_explicit_n(capsys, monkeypatch):
    monkeypatch.setenv("BANREP_MAX_REPORTS", "no_es_entero")
    monkeypatch.setattr("sys.argv", ["banrep-rag", "descubrir", "--max-reports", "1", "--json"])

    def discover(_, maximum):
        assert maximum == 1
        return [
            ReportRecord(
                "ipm-2026-07",
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
    assert json.loads(capsys.readouterr().out)[0]["report_id"] == "ipm-2026-07"


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
