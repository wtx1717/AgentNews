from agentnews import main
from agentnews.config import Settings


def _settings(tmp_path):
    return Settings(_env_file=None, sqlite_path=tmp_path / "cli.sqlite3")


def test_main_starts_the_server_with_defaults(tmp_path, capsys):
    settings = _settings(tmp_path)
    calls = []

    def fake_run_server(app, **kwargs):
        calls.append((app, kwargs))

    main(settings, [], run_server=fake_run_server)

    captured = capsys.readouterr()
    assert captured.out == "AgentNews 正在启动：http://127.0.0.1:8000\n"
    assert captured.err == ""
    assert settings.sqlite_path.exists()
    assert calls == [
        (
            "agentnews.api:app",
            {"host": "127.0.0.1", "port": 8000, "reload": True},
        )
    ]


def test_main_supports_host_port_and_disabling_reload(tmp_path, capsys):
    settings = _settings(tmp_path)
    calls = []

    def fake_run_server(app, **kwargs):
        calls.append((app, kwargs))

    main(
        settings,
        ["serve", "--host", "127.0.0.1", "--port", "8080", "--no-reload"],
        run_server=fake_run_server,
    )

    captured = capsys.readouterr()
    assert captured.out == "AgentNews 正在启动：http://127.0.0.1:8080\n"
    assert calls == [
        (
            "agentnews.api:app",
            {"host": "127.0.0.1", "port": 8080, "reload": False},
        )
    ]


def test_main_init_db_only_initializes_the_database(tmp_path, capsys):
    settings = _settings(tmp_path)

    def unexpected_server(*_args, **_kwargs):
        raise AssertionError("init-db must not start the web server")

    main(settings, ["init-db"], run_server=unexpected_server)

    captured = capsys.readouterr()
    assert captured.out == "AgentNews 数据库已就绪。\n"
    assert captured.err == ""
    assert settings.sqlite_path.exists()
