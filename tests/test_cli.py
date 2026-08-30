from threadgoon.cli import build_parser


def test_version_flag(capsys) -> None:
    parser = build_parser()
    try:
        parser.parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "Threadgoon 6.0.0" in capsys.readouterr().out
