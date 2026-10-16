from politica_monetaria_banrep_rag.cli import main


def test_diagnostico(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["banrep-rag", "diagnostico"])
    main()
    assert "aún no están disponibles" in capsys.readouterr().out
