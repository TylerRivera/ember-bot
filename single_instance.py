"""Только одна копия бота.

Telegram разрешает одному боту только одно подключение — вторая копия получает ошибку Conflict.
Поэтому при запуске новая копия сама закрывает предыдущую (вместе с её чёрным окном).

Что считается предыдущей копией:
  * окно, открытое этим же start_bot.bat (кроме нашего), и запущенный в нём python bot.py;
  * процесс, номер которого записан в bot.pid в папке данных (если бота запускали не через start_bot.bat).
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

PID_FILE_NAME = "bot.pid"
PYTHON_NAMES = ("python.exe", "pythonw.exe")


def _read_pid(data_dir: Path) -> int | None:
    try:
        return int((data_dir / PID_FILE_NAME).read_text().strip())
    except (OSError, ValueError):
        return None


def _windows_processes() -> list[dict]:
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='cmd.exe'\" | "
        "Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = result.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def select_targets(procs: list[dict], base_dir: Path, me: int, parent: int,
                   pid_from_file: int | None) -> tuple[list[int], list[int]]:
    """Возвращает (старые процессы бота, их окна) — что нужно закрыть. Себя и своё окно не трогает."""
    bat = str(base_dir / "start_bot.bat").lower()
    windows = [
        p["ProcessId"] for p in procs
        if (p.get("Name") or "").lower() == "cmd.exe"
        and bat in (p.get("CommandLine") or "").lower()
        and p["ProcessId"] not in (parent, me)
    ]
    bots = [
        p["ProcessId"] for p in procs
        if (p.get("Name") or "").lower() in PYTHON_NAMES
        and "bot.py" in (p.get("CommandLine") or "").lower()
        and p["ProcessId"] not in (me, parent)
        and (p.get("ParentProcessId") in windows or p["ProcessId"] == pid_from_file)
    ]
    return bots, windows


def _linux_previous(pid: int | None, me: int) -> list[int]:
    if not pid or pid == me:
        return []
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [pid] if b"bot.py" in cmdline else []


def replace_previous_instance(base_dir: Path, data_dir: Path | None = None) -> int:
    """Закрывает предыдущую копию бота и записывает свой номер процесса. Возвращает, сколько копий закрыто."""
    data_dir = data_dir or base_dir / "data"
    me, parent = os.getpid(), os.getppid()
    pid_from_file = _read_pid(data_dir)

    if sys.platform == "win32":
        bots, windows = select_targets(_windows_processes(), base_dir, me, parent, pid_from_file)
    else:
        bots, windows = _linux_previous(pid_from_file, me), []

    # Сначала сам бот, потом его окно (иначе окно останется висеть на «Нажмите любую клавишу»).
    for pid in bots + windows:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # процесс уже завершился

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / PID_FILE_NAME).write_text(str(me))
    return len(bots)
