from agentnews import main
from agentnews.config import Settings


def test_main_prints_ready_message(tmp_path, capsys):
    settings = Settings(_env_file=None, sqlite_path=tmp_path / "ready.sqlite3")
    main(settings)

    captured = capsys.readouterr()
    assert captured.out == "AgentNews command-line scaffold is ready.\n"
    assert captured.err == ""


def test_main_initializes_configured_database(tmp_path, capsys):
    settings = Settings(_env_file=None, sqlite_path=tmp_path / "cli.sqlite3")

    main(settings)

    captured = capsys.readouterr()
    assert captured.out == "AgentNews command-line scaffold is ready.\n"
    assert settings.sqlite_path.exists()
