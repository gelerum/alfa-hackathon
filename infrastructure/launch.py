import argparse
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def prepare_env(folder=None):
    folder = folder or HERE
    path = folder / ".env"
    text = path.read_text() if path.exists() else (folder / ".env.example").read_text()
    for key in ("INTERNAL_TOKEN", "ADMIN_TOKEN", "SEARXNG_SECRET"):
        pattern = re.compile(r"^" + key + r"\s*=([^\n]*)$", re.MULTILINE)
        match = pattern.search(text)
        if match and match.group(1).strip() not in ("", '""', "''"):
            continue
        line = key + "=" + secrets.token_urlsafe(48)
        text = pattern.sub(lambda _: line, text, count=1) if match else text.rstrip() + "\n" + line + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    path.chmod(0o600)
    return text


def engine_command():
    requested = os.environ.get("CONTAINER_ENGINE")
    engine = requested or shutil.which("docker") or shutil.which("podman")
    if not engine or not shutil.which(engine):
        raise RuntimeError("Нужен Docker Compose v2 или Podman с Compose provider. Установите их на хосте.")
    return [engine, "compose"]


def main():
    parser = argparse.ArgumentParser(description="Запуск единого локального стенда Альфа-Бизнеса")
    parser.add_argument("--ui", action="store_true", help="Только backend, frontend и БД")
    parser.add_argument(
        "--check", action="store_true", help="Подготовить .env и проверить Compose без запуска"
    )
    args = parser.parse_args()
    os.chdir(HERE)
    config = prepare_env()
    command = engine_command()
    subprocess.run(command + ["config", "--quiet"], check=True)
    if args.check:
        print("Конфигурация корректна. Значения секретов не выводились.")
        return
    if re.search(r"^OPENAI_API_KEY\s*=\s*$", config, re.MULTILINE):
        print(
            "В .env не задан OPENAI_API_KEY: интерфейс доступен, но генерация аналитики требует настройки LLM.",
            flush=True,
        )
    subprocess.run(command + ["up", "-d", "--build", "--wait", "--wait-timeout", "600", "db"], check=True)
    initialized = subprocess.check_output(
        command
        + [
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT to_regclass(\'public.jobs\') IS NOT NULL"',
        ],
        text=True,
    ).strip()
    if initialized != "t":
        subprocess.run(command + ["exec", "-T", "db", "sh", "/usr/local/bin/init-empty-database"], check=True)
    subprocess.run(
        command
        + [
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/04_retrieval_indexes.sql',
        ],
        check=True,
    )
    services = ["backend"] if args.ui else []
    subprocess.run(
        command + ["up", "-d", "--build", "--wait", "--wait-timeout", "1200"] + services, check=True
    )

    def setting(name, default):
        match = re.search(r"^" + name + r"\s*=\s*([^\n#]*)", config, re.MULTILINE)
        return match.group(1).strip().strip("\"'") if match else default

    host = os.environ.get("BACKEND_BIND_HOST") or setting("BACKEND_BIND_HOST", "127.0.0.1")
    port = os.environ.get("BACKEND_PORT") or setting("BACKEND_PORT", "8092")
    if host in ("0.0.0.0", "::"):
        host = "localhost"
    elif ":" in host:
        host = "[" + host + "]"
    print(f"\nАльфа-Бизнес: http://{host}:{port}/ — демо-вход автоматический.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print("Запуск остановлен: " + str(error), file=sys.stderr)
        sys.exit(1)
