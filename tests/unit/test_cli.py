"""CLI argument handling that doesn't need media."""
import json

from clipstudio.cli import build_parser, expand_inputs, main


def test_expand_inputs(tmp_path):
    (tmp_path / "b.mp4").write_bytes(b"x")
    (tmp_path / "a.MKV").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "c.partial.mp4").write_bytes(b"x")
    one = tmp_path / "solo.mov"
    one.write_bytes(b"x")
    got = expand_inputs([tmp_path, one, tmp_path / "b.mp4"])
    assert [p.name for p in got] == ["a.MKV", "b.mp4", "solo.mov"]


def test_every_command_accepts_json():
    ap = build_parser()
    for argv in (["doctor"], ["process", "--input", "x.mp4"], ["review", "--project", "p"], ["export", "--project", "p"],
                 ["resume", "--project", "p"], ["status"], ["ui"]):
        assert ap.parse_args(argv + ["--json"]).json is True


def test_json_errors_are_machine_readable(tmp_path, capsys):
    rc = main(["status", "--project", "does-not-exist", "--json", "--set", f"paths.data_dir={tmp_path}"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["exit_code"] == 2 and "does-not-exist" in out["error"]


def test_status_json_lists_projects(tmp_path, capsys):
    assert main(["status", "--json", "--set", f"paths.data_dir={tmp_path}"]) == 0
    assert json.loads(capsys.readouterr().out) == {"projects": []}


def test_unknown_platform_is_a_config_error(tmp_path, capsys):
    rc = main(["export", "--project", "x", "--platform", "myspace", "--json", "--set", f"paths.data_dir={tmp_path}"])
    assert rc == 2 and "myspace" in json.loads(capsys.readouterr().out)["error"]
