"""Номера сертификатов, суммы и имена: проверка ввода и форматирование."""

import re

CODE_LENGTH = 8
EXAMPLE_CODE = "12345678"


def normalize_code(text: str) -> str | None:
    """Номер сертификата — ровно 8 цифр. Пробелы, дефисы и знак «№» при вводе допускаются.

    '1234 5678', '№12345678' -> '12345678'. None, если это не номер.
    """
    s = re.sub(r"[\s\-–—№#]", "", text or "")
    return s if re.fullmatch(rf"[0-9]{{{CODE_LENGTH}}}", s) else None


MAX_AMOUNT = 10_000_000


def parse_amount(text: str) -> int | None:
    """'5000', '5 000', '5000 руб', '5000₽' -> 5000. None, если не сумма."""
    s = text.lower().replace(" ", "").replace(" ", "")
    s = re.sub(r"(₽|руб(лей|ля|ль)?\.?|р\.?)$", "", s)
    if not re.fullmatch(r"[0-9]{1,8}", s):
        return None
    value = int(s)
    if not 1 <= value <= MAX_AMOUNT:
        return None
    return value


def format_amount(value: int, space: str = " ") -> str:
    return f"{value:,}".replace(",", space) + f"{space}₽"


MAX_ISSUER_LENGTH = 30  # длиннее — на линии «Управляющий» станет слишком мелко


def clean_issuer(text: str) -> str | None:
    """Имя выдавшего: убираем лишние пробелы. None, если пусто или слишком длинно."""
    s = " ".join((text or "").split())
    return s if 1 <= len(s) <= MAX_ISSUER_LENGTH else None
