from tools.environments import shell_utils


def test_cmd_mode_uses_cmd_d_flag_and_no_shell(monkeypatch):
    monkeypatch.setattr(shell_utils, "get_local_shell_mode", lambda: "cmd")
    monkeypatch.setattr(shell_utils.os, "name", "nt", raising=False)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    args, kwargs, mode = shell_utils.build_local_subprocess_invocation(
        command="echo hello",
        work_dir=r"C:\tmp",
    )

    assert mode == "cmd"
    assert kwargs["shell"] is False
    assert args[0].lower().endswith("cmd.exe")
    lowered = [a.lower() for a in args]
    assert "/d" in lowered
    assert "/c" in lowered
    assert any('cd /d "c:\\tmp"' in part.lower() for part in args)
