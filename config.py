"""Настройки бота. Значения берутся из файла .env рядом с этим файлом."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", ""))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow")

# Папку с базой можно задать переменной DATA_DIR (на хостинге — постоянный диск).
DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or BASE_DIR / "data")
DB_PATH = DATA_DIR / "certificates.db"
FRONT_TEMPLATE = BASE_DIR / "assets" / "front.png"  # лицевая сторона: сумма
BACK_TEMPLATE = BASE_DIR / "assets" / "back.png"  # оборот: номер и кто выдал
FONTS_DIR = BASE_DIR / "assets" / "fonts"
