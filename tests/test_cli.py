from agentnews import main


def test_main_prints_ready_message(capsys):
    main()

    captured = capsys.readouterr()
    assert captured.out == "AgentNews command-line scaffold is ready.\n"
    assert captured.err == ""
