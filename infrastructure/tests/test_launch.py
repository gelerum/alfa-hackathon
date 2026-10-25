import importlib.util
from pathlib import Path
import stat

SPEC = importlib.util.spec_from_file_location(
    "stack_launch", Path(__file__).resolve().parents[1] / "launch.py"
)
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)


def test_env_setup_is_private_idempotent_and_preserves_configuration(tmp_path):
    (tmp_path / ".env.example").write_text(
        "INTERNAL_TOKEN=\nADMIN_TOKEN=\nSEARXNG_SECRET=\nOPENAI_API_KEY=\nDEMO_MODE=true\n"
    )
    first = launch.prepare_env(tmp_path)
    assert all(line.split("=", 1)[1] for line in first.splitlines()[:3])
    assert len({line.split("=", 1)[1] for line in first.splitlines()[:3]}) == 3
    assert launch.prepare_env(tmp_path) == first
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    text = first.replace("OPENAI_API_KEY=", "OPENAI_API_KEY=private-test-key")
    (tmp_path / ".env").write_text(text)
    assert launch.prepare_env(tmp_path) == text


def test_ui_launch_keeps_data_and_recovers_only_empty_database(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launch, "HERE", tmp_path)
    monkeypatch.setattr(launch.sys, "argv", ["launch.py", "--ui"])
    monkeypatch.setenv("CONTAINER_ENGINE", "podman")
    monkeypatch.setattr(launch.shutil, "which", lambda value: "/usr/bin/podman")
    calls = []
    monkeypatch.setattr(launch.subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(launch.subprocess, "check_output", lambda *args, **kwargs: "f\n")
    launch.main()
    assert calls[0][-2:] == ["config", "--quiet"]
    assert any("/usr/local/bin/init-empty-database" in command for command in calls)
    assert calls[-1][-1] == "backend"
    assert not any("down" in command for command in calls)
