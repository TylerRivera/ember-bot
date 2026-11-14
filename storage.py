"""Хранение сертификатов и сотрудников в SQLite (один файл data/certificates.db)."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Certificate:
    code: str
    amount: int
    created_at: datetime
    created_by: int
    created_by_name: str | None  # кто создал в боте (имя в Telegram)
    issued_by_name: str | None  # кто выдал — напечатано на сертификате
    used_at: datetime | None
    used_by: int | None
    used_by_name: str | None

    @property
    def is_used(self) -> bool:
        return self.used_at is not None


@dataclass(frozen=True)
class Employee:
    user_id: int
    name: str


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS certificates (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                code            TEXT NOT NULL UNIQUE,
                amount          INTEGER NOT NULL,
                created_at      TEXT NOT NULL,
                created_by      INTEGER NOT NULL,
                created_by_name TEXT,
                issued_by_name  TEXT,
                used_at         TEXT,
                used_by         INTEGER,
                used_by_name    TEXT
            );
            CREATE TABLE IF NOT EXISTS staff (
                user_id  INTEGER PRIMARY KEY,
                name     TEXT NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id  INTEGER PRIMARY KEY,
                name     TEXT NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS last_issuer (
                user_id INTEGER PRIMARY KEY,
                name    TEXT NOT NULL
            );
            """
        )
        # Базы, созданные прошлой версией бота, дополняем новыми столбцами.
        columns = {r["name"] for r in self._db.execute("PRAGMA table_info(certificates)")}
        for column in ("created_by_name", "issued_by_name"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE certificates ADD COLUMN {column} TEXT")

    # --- сертификаты -------------------------------------------------------

    def create_certificate(self, code: str, amount: int, created_by: int, created_by_name: str,
                           issued_by_name: str | None, now: datetime) -> Certificate | None:
        """Создаёт сертификат. None — если сертификат с таким номером уже есть."""
        try:
            self._db.execute(
                "INSERT INTO certificates (code, amount, created_at, created_by, created_by_name, issued_by_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (code, amount, now.isoformat(), created_by, created_by_name, issued_by_name),
            )
        except sqlite3.IntegrityError:
            return None
        return self.get(code)

    def get(self, code: str) -> Certificate | None:
        row = self._db.execute("SELECT * FROM certificates WHERE code = ?", (code,)).fetchone()
        return _to_certificate(row) if row else None

    def redeem(self, code: str, user_id: int, user_name: str, now: datetime) -> bool:
        """Отмечает сертификат использованным. False — если его нет или он уже использован.

        Проверка и отметка — один запрос, поэтому два сотрудника не смогут погасить
        один сертификат одновременно.
        """
        cur = self._db.execute(
            "UPDATE certificates SET used_at = ?, used_by = ?, used_by_name = ? "
            "WHERE code = ? AND used_at IS NULL",
            (now.isoformat(), user_id, user_name, code),
        )
        return cur.rowcount == 1

    def restore(self, code: str) -> bool:
        """Снимает отметку об использовании (если погасили по ошибке)."""
        cur = self._db.execute(
            "UPDATE certificates SET used_at = NULL, used_by = NULL, used_by_name = NULL "
            "WHERE code = ? AND used_at IS NOT NULL",
            (code,),
        )
        return cur.rowcount == 1

    def delete(self, code: str) -> bool:
        return self._db.execute("DELETE FROM certificates WHERE code = ?", (code,)).rowcount == 1

    def all_certificates(self) -> list[Certificate]:
        rows = self._db.execute("SELECT * FROM certificates ORDER BY id DESC").fetchall()
        return [_to_certificate(r) for r in rows]

    # --- кто выдал: запоминаем последнее имя, чтобы не вводить каждый раз ----------

    def last_issuer(self, user_id: int) -> str | None:
        row = self._db.execute("SELECT name FROM last_issuer WHERE user_id = ?", (user_id,)).fetchone()
        return row["name"] if row else None

    def set_last_issuer(self, user_id: int, name: str) -> None:
        self._db.execute(
            "INSERT INTO last_issuer (user_id, name) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name),
        )

    # --- сотрудники --------------------------------------------------------

    def is_staff(self, user_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM staff WHERE user_id = ?", (user_id,)).fetchone() is not None

    def add_staff(self, user_id: int, name: str, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO staff (user_id, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name, now.isoformat()),
        )

    def remove_staff(self, user_id: int) -> bool:
        return self._db.execute("DELETE FROM staff WHERE user_id = ?", (user_id,)).rowcount == 1

    def list_staff(self) -> list[Employee]:
        rows = self._db.execute("SELECT user_id, name FROM staff ORDER BY name").fetchall()
        return [Employee(r["user_id"], r["name"]) for r in rows]

    def staff_name(self, user_id: int) -> str | None:
        row = self._db.execute("SELECT name FROM staff WHERE user_id = ?", (user_id,)).fetchone()
        return row["name"] if row else None

    # --- администраторы, назначенные из бота (главные — в .env, ADMIN_IDS) ----------

    def is_admin(self, user_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone() is not None

    def add_admin(self, user_id: int, name: str, now: datetime) -> None:
        """Назначает администратором (если человек был сотрудником — переводит из сотрудников)."""
        self._db.execute(
            "INSERT INTO admins (user_id, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name, now.isoformat()),
        )
        self._db.execute("DELETE FROM staff WHERE user_id = ?", (user_id,))

    def demote_admin(self, user_id: int, now: datetime) -> str | None:
        """Снимает права администратора, человек остаётся сотрудником. Возвращает имя или None."""
        row = self._db.execute("SELECT name FROM admins WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        self._db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        self.add_staff(user_id, row["name"], now)
        return row["name"]

    def list_admins(self) -> list[Employee]:
        rows = self._db.execute("SELECT user_id, name FROM admins ORDER BY name").fetchall()
        return [Employee(r["user_id"], r["name"]) for r in rows]


def _to_certificate(row: sqlite3.Row) -> Certificate:
    return Certificate(
        code=row["code"],
        amount=row["amount"],
        created_at=datetime.fromisoformat(row["created_at"]),
        created_by=row["created_by"],
        created_by_name=row["created_by_name"],
        issued_by_name=row["issued_by_name"],
        used_at=datetime.fromisoformat(row["used_at"]) if row["used_at"] else None,
        used_by=row["used_by"],
        used_by_name=row["used_by_name"],
    )
