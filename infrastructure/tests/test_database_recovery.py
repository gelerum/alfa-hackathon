import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "init-empty-database.sh"


@pytest.fixture
def recovery(tmp_path):
    psql = tmp_path / "psql"
    psql.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        'if "-Atc" in sys.argv:\n'
        '    print(os.environ["MOCK_TABLES"])\n'
        "else:\n"
        '    file = sys.argv[sys.argv.index("-f") + 1]\n'
        '    with Path(os.environ["MOCK_LOG"]).open("a") as log:\n'
        '        log.write(file + "\\n")\n'
        '    if os.environ.get("MOCK_FAIL") in file:\n'
        "        sys.exit(2)\n"
    )
    psql.chmod(0o755)
    log = tmp_path / "calls.log"

    def run(tables="0", fail="never-match"):
        env = dict(
            os.environ,
            PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
            POSTGRES_USER="test",
            POSTGRES_DB="test",
            MOCK_TABLES=tables,
            MOCK_LOG=str(log),
            MOCK_FAIL=fail,
        )
        result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return run


def test_empty_database_initializes_in_order(recovery):
    result, calls = recovery()
    assert result.returncode == 0, result.stderr
    assert [Path(file).name for file in calls] == ["01_schema.sql", "02_import.sql", "03_application.sql"]


def test_existing_database_is_not_modified(recovery):
    result, calls = recovery(tables="12")
    assert result.returncode != 0
    assert "Refusing recovery" in result.stderr
    assert calls == []


def test_sql_error_stops_recovery(recovery):
    result, calls = recovery(fail="02_import.sql")
    assert result.returncode != 0
    assert len(calls) == 2
    assert not any("03_application.sql" in file for file in calls)
