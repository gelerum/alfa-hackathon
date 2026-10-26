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


def setting(config, name, default=""):
    value = os.environ.get(name)
    if value is not None:
        return value.strip() or default
    match = re.search(r"^" + name + r"[ \t]*=[ \t]*([^\n#]*)", config, re.MULTILINE)
    return (match.group(1).strip().strip("\"'") or default) if match else default


def model_environment(config, local_qwen):
    env = dict(os.environ)
    if local_qwen:
        env.update(
            OPENAI_BASE_URL="http://qwen-llm:8080/v1",
            OPENAI_API_KEY=setting(config, "INTERNAL_TOKEN"),
            OPENAI_MODEL="qwen3.6-35b-a3b",
            OPENAI_DISABLE_THINKING="true",
            OPENAI_TIMEOUT_SECONDS=setting(config, "QWEN_API_TIMEOUT_SECONDS", "600"),
            AGENT_TIMEOUT_SECONDS=setting(config, "QWEN_AGENT_TIMEOUT_SECONDS", "1800"),
            AGENT_MAX_CONTEXT_BYTES="60000",
            AGENT_MAX_TOTAL_TOKENS="300000",
        )
    return env


def main():
    parser = argparse.ArgumentParser(description="Запуск единого локального стенда Альфа-Бизнеса")
    parser.add_argument("--ui", action="store_true", help="Только backend, frontend и БД")
    parser.add_argument(
        "--check", action="store_true", help="Подготовить .env и проверить Compose без запуска"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--with-qwen", action="store_true", help="Запустить генеративную Qwen3.6-35B-A3B в Compose"
    )
    modes.add_argument(
        "--without-qwen", action="store_true", help="Использовать внешний OPENAI endpoint (по умолчанию)"
    )
    args = parser.parse_args()
    os.chdir(HERE)
    config = prepare_env()
    command = engine_command()
    local_qwen = args.with_qwen and not args.ui
    runtime_env = model_environment(config, local_qwen)
    if local_qwen:
        command += ["--profile", "local-llm"]
    subprocess.run(command + ["config", "--quiet"], check=True, env=runtime_env)
    if args.check:
        print("Конфигурация корректна. Значения секретов не выводились.")
        return
    if not local_qwen and (not setting(config, "OPENAI_API_KEY") or not setting(config, "OPENAI_BASE_URL")):
        print(
            "В .env не настроены OPENAI_BASE_URL / OPENAI_API_KEY: интерфейс доступен, но генерация аналитики требует настройки LLM.",
            flush=True,
        )
    subprocess.run(
        command + ["up", "-d", "--build", "--wait", "--wait-timeout", "600", "db"],
        check=True,
        env=runtime_env,
    )
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
        env=runtime_env,
    ).strip()
    if initialized != "t":
        subprocess.run(
            command + ["exec", "-T", "db", "sh", "/usr/local/bin/init-empty-database"],
            check=True,
            env=runtime_env,
        )
    for migration in ("04_retrieval_indexes.sql", "05_observability.sql"):
        subprocess.run(
            command
            + [
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                f"-f /docker-entrypoint-initdb.d/{migration}",
            ],
            check=True,
            env=runtime_env,
        )
    if not local_qwen and not args.ui:
        subprocess.run(command + ["--profile", "local-llm", "stop", "qwen-llm"], check=True, env=runtime_env)
    if local_qwen:
        timeout = setting(config, "QWEN_STARTUP_TIMEOUT_SECONDS", "3600")
        print(
            "Загрузка Qwen3.6-35B-A3B (~22 ГБ при первом запуске). Логи: docker compose --profile local-llm logs -f qwen-llm",
            flush=True,
        )
        subprocess.run(
            command + ["up", "-d", "--wait", "--wait-timeout", timeout, "qwen-llm"],
            check=True,
            env=runtime_env,
        )
    services = ["backend"] if args.ui else []
    subprocess.run(
        command + ["up", "-d", "--build", "--wait", "--wait-timeout", "1200"] + services,
        check=True,
        env=runtime_env,
    )

    host = setting(config, "BACKEND_BIND_HOST", "127.0.0.1")
    port = setting(config, "BACKEND_PORT", "8092")
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
