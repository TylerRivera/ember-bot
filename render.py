"""Рисует сертификат по двум шаблонам.

Лицевая сторона (assets/front.png): сумма на линии «на сумму ____ ₽».
Оборот (assets/back.png): номер после «№» и имя выдавшего на линии «Управляющий ____».
"""

import os
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import BACK_TEMPLATE, FONTS_DIR, FRONT_TEMPLATE

INK = (24, 56, 88)  # тёмно-синий, как текст шаблона

# Координаты ниже — в сетке исходных шаблонов 1024×484 и масштабируются под реальный размер.
REF_W = 1024
FRONT_AMOUNT_LINE = (249, 411, 286)  # x0, x1, y линии «на сумму ____ ₽»
BACK_NUMBER_BOX = (80, 402, 196, 452)  # место после «№»: закрашиваем фоном и пишем номер
BACK_NUMBER_X, BACK_NUMBER_BASELINE, BACK_NUMBER_MAX_W = 86, 434, 104
BACK_MANAGER_LINE = (412, 623, 434)  # x0, x1, y линии «Управляющий ____»
SIZE_AMOUNT, SIZE_NUMBER, SIZE_ISSUER = 46, 32, 26

OUTPUT_MIN_WIDTH = 2048  # шаблоны небольшие — увеличиваем, чтобы вписанный текст был чётким при печати
PRINT_WIDTH_MM = 210  # ширина листа в PDF (шаблон 1024×484 ≈ 210×99 мм, формат «евро»)

_WIN_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# Свой (фирменный) шрифт можно положить в assets/fonts/regular.ttf — он будет использоваться первым.
# Основной — Golos Text (открытая лицензия OFL, лежит в проекте), поэтому на компьютере и на сервере
# сертификаты выглядят одинаково. Остальные — запасные, если файла шрифта вдруг нет.
FONT_CANDIDATES = [
    (FONTS_DIR / "regular.ttf", None),
    (FONTS_DIR / "GolosText-wght.ttf", 400),  # насыщенность 400 — как текст шаблона
    (_WIN_FONTS / "SegUIVar.ttf", 500),
    (_WIN_FONTS / "segoeui.ttf", None),
    (_WIN_FONTS / "arial.ttf", None),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), None),
]


@lru_cache(maxsize=None)
def _font(size: int) -> ImageFont.FreeTypeFont:
    for path, weight in FONT_CANDIDATES:
        if not path.is_file():
            continue
        font = ImageFont.truetype(str(path), size)
        if weight:
            try:
                axes = font.get_variation_axes()
                font.set_variation_by_axes([
                    weight if "eight" in str(a.get("name")) else a["default"] for a in axes
                ])
            except OSError:
                pass  # не вариативный шрифт — оставляем как есть
        return font
    return ImageFont.load_default(size)


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_width: float) -> ImageFont.FreeTypeFont:
    font = _font(size)
    while size > 8 and draw.textlength(text, font=font) > max_width:
        size -= 1
        font = _font(size)
    return font


@lru_cache(maxsize=2)
def _template(path: Path) -> Image.Image:
    img = Image.open(path).convert("RGB")
    if img.width < OUTPUT_MIN_WIDTH:
        k = OUTPUT_MIN_WIDTH / img.width
        img = img.resize((OUTPUT_MIN_WIDTH, round(img.height * k)), Image.LANCZOS)
    return img


def render_front(amount: int) -> Image.Image:
    img = _template(FRONT_TEMPLATE).copy()
    draw = ImageDraw.Draw(img)
    k = img.width / REF_W
    x0, x1, y = FRONT_AMOUNT_LINE
    text = f"{amount:,}".replace(",", " ")
    font = _fit(draw, text, round(SIZE_AMOUNT * k), (x1 - x0 - 10) * k)
    draw.text(((x0 + x1) / 2 * k, (y - 6) * k), text, font=font, fill=INK, anchor="ms")
    return img


def render_back(code: str, issuer: str | None) -> Image.Image:
    img = _template(BACK_TEMPLATE).copy()
    draw = ImageDraw.Draw(img)
    k = img.width / REF_W
    background = img.getpixel((img.width - 5, img.height - 5))

    bx0, by0, bx1, by1 = BACK_NUMBER_BOX
    draw.rectangle((bx0 * k, by0 * k, bx1 * k, by1 * k), fill=background)
    font = _fit(draw, code, round(SIZE_NUMBER * k), BACK_NUMBER_MAX_W * k)
    draw.text((BACK_NUMBER_X * k, BACK_NUMBER_BASELINE * k), code, font=font, fill=INK, anchor="ls")

    if issuer:
        x0, x1, y = BACK_MANAGER_LINE
        font = _fit(draw, issuer, round(SIZE_ISSUER * k), (x1 - x0 - 8) * k)
        draw.text(((x0 + x1) / 2 * k, (y - 5) * k), issuer, font=font, fill=INK, anchor="ms")
    return img


def to_jpeg(img: Image.Image) -> bytes:
    out = BytesIO()
    img.save(out, format="JPEG", quality=93)
    return out.getvalue()


def to_pdf(pages: list[Image.Image]) -> bytes:
    """Многостраничный PDF: страница = сторона сертификата, ширина PRINT_WIDTH_MM."""
    dpi = pages[0].width / (PRINT_WIDTH_MM / 25.4)
    out = BytesIO()
    pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:], resolution=dpi, quality=95)
    return out.getvalue()
