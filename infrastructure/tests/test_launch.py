import importlib.util
from pathlib import Path
import stat
import os
import subprocess

import pytest

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


def test_ui_launch_keeps_data_and_recovers_only_empty_database(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env.example").write_text(
        "OPENAI_API_KEY=\nBACKEND_BIND_HOST=100.64.0.1\nBACKEND_PORT=8092\n"
    )
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
    assert "http://100.64.0.1:8092/" in capsys.readouterr().out


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_engine_wrapper_selects_engine_and_forwards_arguments(tmp_path, engine):
    python = tmp_path / "python3"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$CONTAINER_ENGINE" "$PWD" "$@"\n')
    python.chmod(0o755)
    folder = Path(__file__).resolve().parents[1]
    env = dict(
        os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], CONTAINER_ENGINE="wrong-engine"
    )
    result = subprocess.run(
        [str(folder / f"run-{engine}.sh"), "--ui", "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [engine, str(folder), "launch.py", "--ui", "--check"]
