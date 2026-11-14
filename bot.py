"""Telegram-бот Ember: выпуск подарочных сертификатов и их погашение.

Роли:
  * сотрудник (добавляет администратор) — создаёт сертификаты (номер, сумма, кто выдал)
    и проверяет номера; действующий сертификат при проверке сразу гасится;
  * администратор — всё то же, плюс список сертификатов, выгрузка в Excel,
    управление командой, отмена погашения и удаление сертификата.
    Главные администраторы указаны в .env (ADMIN_IDS), остальных назначают кнопками в боте.
"""

import asyncio
import csv
import html
import io
import logging
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from aiogram.utils.token import TokenValidationError

import config
from codes import (
    CODE_LENGTH,
    EXAMPLE_CODE,
    MAX_AMOUNT,
    MAX_ISSUER_LENGTH,
    clean_issuer,
    format_amount,
    normalize_code,
    parse_amount,
)
from render import render_back, render_front, to_jpeg, to_pdf
from single_instance import replace_previous_instance
from storage import Certificate, Storage

log = logging.getLogger("ember_bot")
storage = Storage(config.DB_PATH)

BTN_CREATE = "🎟 Создать сертификат"
BTN_CHECK = "🔎 Проверить номер"
BTN_LIST = "📋 Все сертификаты"
BTN_STAFF = "👥 Сотрудники"
MENU_BUTTONS = {BTN_CREATE, BTN_CHECK, BTN_LIST, BTN_STAFF}

LIST_LIMIT = 40  # сколько сертификатов показывать в сообщении; полный список — в файле
EXAMPLE_ISSUER = "Иванова А. С."

STAFF_HELP = (
    "<b>Что умеет бот</b>\n"
    f"{BTN_CREATE} — введите номер ({CODE_LENGTH} цифр), сумму и кто выдал. "
    "Бот пришлёт обе стороны сертификата и PDF для печати.\n"
    f"{BTN_CHECK} — отправьте номер сертификата. Если он действует, бот сразу отметит его использованным.\n\n"
    f"Быстрое создание одной строкой: <code>/new {EXAMPLE_CODE} 5000 {EXAMPLE_ISSUER}</code>"
)
ADMIN_HELP = STAFF_HELP + (
    f"\n\n{BTN_LIST} — список со статусами и выгрузка в Excel.\n"
    f"{BTN_STAFF} — сотрудники и администраторы: добавить, назначить админом, убрать.\n\n"
    "<b>Команды администратора</b>\n"
    f"/restore {EXAMPLE_CODE} — вернуть сертификат в действующие, если его погасили по ошибке\n"
    f"/delete {EXAMPLE_CODE} — удалить сертификат (например, если ошиблись в номере или сумме)\n"
    "/addstaff ID Имя — добавить сотрудника\n"
    "/delstaff ID — убрать сотрудника\n"
    "/addadmin ID Имя — назначить администратора\n"
    "/deladmin ID — снять права администратора (человек останется сотрудником)\n"
    "/cancel — отменить текущее действие"
)


# --- вспомогательное ---------------------------------------------------------


def now() -> datetime:
    return datetime.now(config.TIMEZONE)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(config.TIMEZONE).strftime("%d.%m.%Y %H:%M")


def fmt_d(dt: datetime) -> str:
    return dt.astimezone(config.TIMEZONE).strftime("%d.%m.%Y")


def h(text: str | None) -> str:
    return html.escape(text or "")


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS or storage.is_admin(user_id)


def all_admin_ids() -> set[int]:
    return set(config.ADMIN_IDS) | {a.user_id for a in storage.list_admins()}


def is_staff(user_id: int) -> bool:
    return is_admin(user_id) or storage.is_staff(user_id)


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and is_admin(event.from_user.id)


class IsStaff(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and is_staff(event.from_user.id)


def menu(user_id: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_CREATE), KeyboardButton(text=BTN_CHECK)]]
    if is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_STAFF)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def split_message(text: str, limit: int = 4000) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def status_word(cert: Certificate) -> str:
    return "уже использован" if cert.is_used else "действует"


def cert_details(cert: Certificate) -> str:
    issuer = f"\nВыдал: {h(cert.issued_by_name)}" if cert.issued_by_name else ""
    return (
        f"Номер: <code>{h(cert.code)}</code>\n"
        f"Сумма: <b>{format_amount(cert.amount)}</b>\n"
        f"Дата выдачи: {fmt_d(cert.created_at)}{issuer}"
    )


class CreateCert(StatesGroup):
    number = State()
    amount = State()
    issuer = State()


# Обычный текст, введённый пользователем (не кнопка меню и не команда).
TEXT_INPUT = (F.text, ~F.text.in_(MENU_BUTTONS), ~F.text.startswith("/"))

private_only = F.chat.type == ChatType.PRIVATE

common = Router(name="common")
common.message.filter(private_only)

admin = Router(name="admin")
admin.message.filter(private_only, IsAdmin())
admin.callback_query.filter(IsAdmin())

staff = Router(name="staff")
staff.message.filter(private_only, IsStaff())
staff.callback_query.filter(IsStaff())

fallback = Router(name="fallback")
fallback.message.filter(private_only)

# Кто нажал /start, но ещё не сотрудник: {user_id: имя}. Нужен для кнопки «Сделать сотрудником».
pending_requests: dict[int, str] = {}


# --- общие команды (для всех) --------------------------------------------------


@common.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    user = message.from_user
    if not all_admin_ids():
        await message.answer(
            "Бот ещё не настроен.\n\n"
            f"Ваш Telegram ID: <code>{user.id}</code>\n\n"
            "Впишите его в файл <b>.env</b> в строку <code>ADMIN_IDS=</code> и перезапустите бота — "
            "после этого вы станете администратором."
        )
        return
    if is_admin(user.id):
        await message.answer("Здравствуйте! Вы администратор.\n\n" + ADMIN_HELP, reply_markup=menu(user.id))
    elif storage.is_staff(user.id):
        await message.answer("Здравствуйте!\n\n" + STAFF_HELP, reply_markup=menu(user.id))
    else:
        await message.answer(
            "Этот бот только для сотрудников Ember.\n\n"
            f"Ваш Telegram ID: <code>{user.id}</code>\n"
            "Запрос на доступ отправлен администратору. После одобрения вам придёт сообщение.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await request_access(bot, user)


async def request_access(bot: Bot, user: User) -> None:
    if user.id in pending_requests:
        return
    name = user.full_name or f"ID {user.id}"
    pending_requests[user.id] = name
    username = f" (@{user.username})" if user.username else ""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сотрудник", callback_data=f"staff_add:{user.id}"),
            InlineKeyboardButton(text="⭐ Администратор", callback_data=f"adm_add:{user.id}"),
        ],
        [InlineKeyboardButton(text="✖️ Отклонить", callback_data=f"staff_deny:{user.id}")],
    ])
    for admin_id in all_admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"🔔 Запрос доступа к боту\n<b>{h(name)}</b>{h(username)}\nID: <code>{user.id}</code>",
                reply_markup=keyboard,
            )
        except TelegramAPIError as exc:
            log.warning("Cannot notify admin %s: %s", admin_id, exc)


@common.message(Command("help"))
async def cmd_help(message: Message) -> None:
    uid = message.from_user.id
    if is_admin(uid):
        await message.answer(ADMIN_HELP, reply_markup=menu(uid))
    elif is_staff(uid):
        await message.answer(STAFF_HELP, reply_markup=menu(uid))
    else:
        await message.answer("Этот бот только для сотрудников. Нажмите /start, чтобы запросить доступ.")


@common.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@common.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    await message.answer("Отменено.", reply_markup=menu(uid) if is_staff(uid) else None)


# --- администратор: список сертификатов ------------------------------------------


def cert_line(cert: Certificate) -> str:
    head = f"<code>{h(cert.code)}</code> — {format_amount(cert.amount)}"
    if cert.is_used:
        who = f", {h(cert.used_by_name)}" if cert.used_by_name else ""
        return f"⚪️ {head} — использован {fmt_d(cert.used_at)}{who}"
    issuer = f", {h(cert.issued_by_name)}" if cert.issued_by_name else ""
    return f"🟢 {head} — действует · выдан {fmt_d(cert.created_at)}{issuer}"


@admin.message(F.text == BTN_LIST)
@admin.message(Command("list"))
async def show_list(message: Message, state: FSMContext) -> None:
    await state.clear()
    certs = storage.all_certificates()
    if not certs:
        await message.answer(f"Сертификатов пока нет. Создайте первый кнопкой «{BTN_CREATE}».")
        return
    active = [c for c in certs if not c.is_used]
    text = (
        "📋 <b>Сертификаты</b>\n"
        f"Всего: {len(certs)} · действуют: {len(active)} · использованы: {len(certs) - len(active)}\n"
        f"Сумма действующих: {format_amount(sum(c.amount for c in active))}\n\n"
        + "\n".join(cert_line(c) for c in certs[:LIST_LIMIT])
    )
    if len(certs) > LIST_LIMIT:
        text += f"\n\n…и ещё {len(certs) - LIST_LIMIT}. Полный список — в файле Excel."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Скачать список (Excel)", callback_data="export"),
    ]])
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=keyboard if i == len(chunks) - 1 else None)


def excel_text(value: str | None) -> str:
    """Имена приходят из Telegram: текст, начинающийся с = + - @, Excel принял бы за формулу."""
    value = value or ""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@admin.callback_query(F.data == "export")
async def export_list(call: CallbackQuery, bot: Bot) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")  # «;» — чтобы русский Excel сразу разложил по столбцам
    writer.writerow([
        "Номер", "Сумма, руб.", "Статус", "Дата выдачи", "Кто выдал",
        "Создал в боте", "Дата использования", "Кто погасил",
    ])
    for c in storage.all_certificates():
        writer.writerow([
            f'="{c.code}"',  # так Excel не съест ведущие нули у номера
            c.amount,
            "использован" if c.is_used else "действует",
            fmt_dt(c.created_at),
            excel_text(c.issued_by_name),
            excel_text(c.created_by_name),
            fmt_dt(c.used_at) if c.used_at else "",
            excel_text(c.used_by_name),
        ])
    await call.answer()
    await bot.send_document(
        call.from_user.id,
        BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=f"certificates_{now():%Y-%m-%d}.csv"),
        caption="Список сертификатов — открывается в Excel",
    )


# --- администратор: отмена погашения и удаление -------------------------------------


@admin.message(Command("restore"))
async def cmd_restore(message: Message, command: CommandObject) -> None:
    code = normalize_code(command.args or "")
    if code is None:
        await message.answer(f"Укажите номер. Пример: <code>/restore {EXAMPLE_CODE}</code>")
        return
    cert = storage.get(code)
    if cert is None:
        await message.answer(f"❌ Сертификат <code>{code}</code> не найден.")
    elif not cert.is_used:
        await message.answer(f"Сертификат <code>{code}</code> и так действует.")
    else:
        storage.restore(code)
        await message.answer(
            f"↩️ Сертификат <code>{code}</code> на {format_amount(cert.amount)} снова действует."
        )


@admin.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(f"Укажите номер. Пример: <code>/delete {EXAMPLE_CODE}</code>")
        return
    code = normalize_code(arg) or arg.upper()  # .upper() — для старых номеров вида EMB-XXXX-XXXX
    cert = storage.get(code)
    if cert is None:
        await message.answer(f"❌ Сертификат <code>{h(arg)}</code> не найден.")
        return
    await message.answer(
        f"Удалить сертификат?\n\n{cert_details(cert)}\nСтатус: {status_word(cert)}\n\n"
        "Удаление нельзя отменить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"del:{cert.code}"),
            InlineKeyboardButton(text="Отмена", callback_data="del_cancel"),
        ]]),
    )


@admin.callback_query(F.data.startswith("del:"))
async def delete_cb(call: CallbackQuery) -> None:
    code = call.data.split(":", 1)[1]
    deleted = storage.delete(code)
    await call.answer("Удалено" if deleted else "Уже удалён")
    if isinstance(call.message, Message):
        await call.message.edit_text(f"🗑 Сертификат <code>{h(code)}</code> удалён.")


@admin.callback_query(F.data == "del_cancel")
async def delete_cancel_cb(call: CallbackQuery) -> None:
    await call.answer("Отменено")
    if isinstance(call.message, Message):
        await call.message.edit_text("Удаление отменено.")


# --- администратор: сотрудники ----------------------------------------------------


def team_view(viewer_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    you = lambda uid: " — <i>это вы</i>" if uid == viewer_id else ""  # noqa: E731
    admins = storage.list_admins()
    employees = storage.list_staff()

    lines = ["👥 <b>Команда</b>", "", f"⭐ <b>Администраторы ({len(config.ADMIN_IDS) + len(admins)})</b>"]
    lines += [f"• ID <code>{uid}</code> — главный, указан в .env{you(uid)}" for uid in sorted(config.ADMIN_IDS)]
    lines += [f"• {h(a.name)} — <code>{a.user_id}</code>{you(a.user_id)}" for a in admins]
    lines += ["", f"👤 <b>Сотрудники ({len(employees)})</b>"]
    lines += [f"• {h(e.name)} — <code>{e.user_id}</code>" for e in employees] or ["пока нет"]
    lines += [
        "",
        "Сотрудники создают и проверяют сертификаты. Администраторы, кроме этого, видят список, "
        "делают выгрузку, удаляют сертификаты и управляют командой.",
        "",
        "<b>Как добавить человека:</b> пусть откроет бота и нажмёт «Старт» — вам придёт запрос "
        "с кнопками «Сотрудник» и «Администратор».\n"
        "Или командами <code>/addstaff ID Имя</code> и <code>/addadmin ID Имя</code>",
    ]

    rows = [
        [InlineKeyboardButton(text=f"⬇️ Снять админа: {a.name[:25]}", callback_data=f"adm_del:{a.user_id}")]
        for a in admins
    ]
    rows += [
        [
            InlineKeyboardButton(text=f"⭐ В админы: {e.name[:20]}", callback_data=f"adm_add:{e.user_id}"),
            InlineKeyboardButton(text="✖️ Удалить", callback_data=f"staff_del:{e.user_id}"),
        ]
        for e in employees
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def refresh_team(call: CallbackQuery) -> None:
    if isinstance(call.message, Message):
        text, keyboard = team_view(call.from_user.id)
        await call.message.edit_text(text, reply_markup=keyboard)


@admin.message(F.text == BTN_STAFF)
@admin.message(Command("staff"))
async def show_staff(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = team_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


async def notify(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=menu(user_id))
    except TelegramAPIError:
        pass  # человек ещё не запускал бота — увидит меню, когда нажмёт /start


async def add_employee(bot: Bot, user_id: int, name: str) -> None:
    pending_requests.pop(user_id, None)
    if is_admin(user_id):
        return  # уже администратор — у него и так есть все права сотрудника
    storage.add_staff(user_id, name, now())
    await notify(bot, user_id, "✅ Вам открыт доступ к боту.\n\n" + STAFF_HELP)


async def add_admin(bot: Bot, user_id: int, name: str) -> None:
    pending_requests.pop(user_id, None)
    if user_id in config.ADMIN_IDS or storage.is_admin(user_id):
        return
    storage.add_admin(user_id, name, now())
    await notify(bot, user_id, "⭐ Вы назначены администратором бота.\n\n" + ADMIN_HELP)


def known_name(user_id: int, given: str | None = None) -> str:
    return given or pending_requests.get(user_id) or storage.staff_name(user_id) or f"ID {user_id}"


@admin.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject, bot: Bot) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.answer(
            "Пример: <code>/addadmin 123456789 Мария</code>\n"
            "ID человек увидит, если откроет бота и нажмёт «Старт»."
        )
        return
    user_id = int(parts[0])
    if is_admin(user_id):
        await message.answer("Этот человек уже администратор.")
        return
    name = known_name(user_id, parts[1].strip() if len(parts) > 1 else None)
    await add_admin(bot, user_id, name)
    await message.answer(f"⭐ Готово: {h(name)} теперь администратор.")


@admin.message(Command("deladmin"))
async def cmd_deladmin(message: Message, command: CommandObject, bot: Bot) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Пример: <code>/deladmin 123456789</code>")
        return
    user_id = int(arg)
    if user_id in config.ADMIN_IDS:
        await message.answer("Это главный администратор — он указан в файле .env, убрать его можно только там.")
        return
    name = storage.demote_admin(user_id, now())
    if name is None:
        await message.answer("Администратора с таким ID нет.")
        return
    await notify(bot, user_id, "Права администратора сняты — вы остаётесь сотрудником.\n\n" + STAFF_HELP)
    await message.answer(f"Готово: {h(name)} больше не администратор, но остаётся сотрудником.")


@admin.callback_query(F.data.startswith("adm_add:"))
async def adm_add_cb(call: CallbackQuery, bot: Bot) -> None:
    user_id = int(call.data.split(":", 1)[1])
    name = known_name(user_id)
    from_request = user_id in pending_requests
    await add_admin(bot, user_id, name)
    await call.answer("Назначен администратором")
    if from_request and isinstance(call.message, Message):
        await call.message.edit_text(f"⭐ {h(name)} теперь администратор (ID <code>{user_id}</code>).")
    else:
        await refresh_team(call)


@admin.callback_query(F.data.startswith("adm_del:"))
async def adm_del_cb(call: CallbackQuery, bot: Bot) -> None:
    user_id = int(call.data.split(":", 1)[1])
    if storage.demote_admin(user_id, now()) is not None:
        await notify(bot, user_id, "Права администратора сняты — вы остаётесь сотрудником.\n\n" + STAFF_HELP)
    await call.answer("Права администратора сняты")
    await refresh_team(call)


@admin.message(Command("addstaff"))
async def cmd_addstaff(message: Message, command: CommandObject, bot: Bot) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.answer(
            "Пример: <code>/addstaff 123456789 Анна</code>\n"
            "ID сотрудник увидит, если откроет бота и нажмёт «Старт»."
        )
        return
    user_id = int(parts[0])
    if is_admin(user_id):
        await message.answer("Этот человек — администратор: права сотрудника у него уже есть.")
        return
    name = known_name(user_id, parts[1].strip() if len(parts) > 1 else None)
    await add_employee(bot, user_id, name)
    await message.answer(f"✅ Готово: {h(name)} теперь сотрудник.")


@admin.message(Command("delstaff"))
async def cmd_delstaff(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Пример: <code>/delstaff 123456789</code>")
        return
    if is_admin(int(arg)):
        await message.answer("Это администратор. Сначала снимите права: <code>/deladmin " + arg + "</code>")
        return
    if storage.remove_staff(int(arg)):
        await message.answer("Сотрудник удалён — работать с ботом он больше не сможет.")
    else:
        await message.answer("Сотрудника с таким ID нет в списке.")


@admin.callback_query(F.data.startswith("staff_add:"))
async def staff_add_cb(call: CallbackQuery, bot: Bot) -> None:
    user_id = int(call.data.split(":", 1)[1])
    name = known_name(user_id)
    await add_employee(bot, user_id, name)
    await call.answer("Добавлено")
    if isinstance(call.message, Message):
        role = "администратор" if is_admin(user_id) else "сотрудник"
        await call.message.edit_text(f"✅ {h(name)} теперь {role} (ID <code>{user_id}</code>).")


@admin.callback_query(F.data.startswith("staff_deny:"))
async def staff_deny_cb(call: CallbackQuery) -> None:
    user_id = int(call.data.split(":", 1)[1])
    name = pending_requests.pop(user_id, f"ID {user_id}")
    await call.answer("Отклонено")
    if isinstance(call.message, Message):
        await call.message.edit_text(f"✖️ Запрос от {h(name)} отклонён.")


@admin.callback_query(F.data.startswith("staff_del:"))
async def staff_del_cb(call: CallbackQuery) -> None:
    storage.remove_staff(int(call.data.split(":", 1)[1]))
    await call.answer("Удалено")
    await refresh_team(call)


# --- сотрудники (и администратор): создание сертификата -------------------------------


def taken_text(cert: Certificate) -> str:
    return (
        f"Сертификат с номером <code>{h(cert.code)}</code> уже есть "
        f"({status_word(cert)}, {format_amount(cert.amount)}). Введите другой номер.\n\nОтмена — /cancel"
    )


@staff.message(F.text == BTN_CREATE)
async def create_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreateCert.number)
    await message.answer(
        f"Введите номер сертификата — ровно {CODE_LENGTH} цифр, например <code>{EXAMPLE_CODE}</code>.\n\n"
        "Отмена — /cancel"
    )


@staff.message(F.text == BTN_CHECK)
async def check_prompt(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(f"Отправьте номер сертификата ({CODE_LENGTH} цифр), например <code>{EXAMPLE_CODE}</code>.")


@staff.message(Command("new"))
async def cmd_new(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if not command.args:
        await create_start(message, state)
        return
    parts = command.args.split(maxsplit=2)
    code = normalize_code(parts[0])
    amount = parse_amount(parts[1]) if len(parts) > 1 else None
    issuer = clean_issuer(parts[2]) if len(parts) > 2 else None
    if code is None or amount is None or (len(parts) > 2 and issuer is None):
        await message.answer(
            "Формат: <code>/new НОМЕР СУММА Кто выдал</code>\n"
            f"Например: <code>/new {EXAMPLE_CODE} 5000 {EXAMPLE_ISSUER}</code>\n\n"
            f"Номер — {CODE_LENGTH} цифр без пробелов, сумма — числом без пробелов, "
            f"кто выдал — до {MAX_ISSUER_LENGTH} символов (можно не указывать)."
        )
        return
    existing = storage.get(code)
    if existing:
        await message.answer(taken_text(existing).replace("Введите другой номер.", "Укажите другой номер."))
        return
    await issue(bot, message.from_user, code, amount, issuer)


@staff.message(StateFilter(CreateCert.number), *TEXT_INPUT)
async def create_number(message: Message, state: FSMContext) -> None:
    code = normalize_code(message.text)
    if code is None:
        await message.answer(
            f"Номер должен состоять ровно из {CODE_LENGTH} цифр, например <code>{EXAMPLE_CODE}</code>. "
            "Попробуйте ещё раз.\n\nОтмена — /cancel"
        )
        return
    existing = storage.get(code)
    if existing:
        await message.answer(taken_text(existing))
        return
    await state.update_data(code=code)
    await state.set_state(CreateCert.amount)
    await message.answer(f"Номер <code>{code}</code> ✔️\n\nТеперь введите сумму в рублях, например <code>5000</code>.")


@staff.message(StateFilter(CreateCert.amount), *TEXT_INPUT)
async def create_amount(message: Message, state: FSMContext) -> None:
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer(
            f"Не понял сумму. Введите число от 1 до {format_amount(MAX_AMOUNT)}, например <code>5000</code>.\n\n"
            "Отмена — /cancel"
        )
        return
    await state.update_data(amount=amount)
    await state.set_state(CreateCert.issuer)
    buttons = []
    last = storage.last_issuer(message.from_user.id)
    if last:
        buttons.append([InlineKeyboardButton(text=f"✍️ {last}", callback_data="issuer:last")])
    buttons.append([InlineKeyboardButton(text="Без имени", callback_data="issuer:none")])
    await message.answer(
        f"Сумма: <b>{format_amount(amount)}</b> ✔️\n\n"
        f"Кто выдал сертификат? Напишите, например: <i>{EXAMPLE_ISSUER}</i>\n"
        "Это будет напечатано на обороте в строке «Управляющий».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@staff.message(StateFilter(CreateCert.issuer), *TEXT_INPUT)
async def create_issuer_text(message: Message, state: FSMContext, bot: Bot) -> None:
    issuer = clean_issuer(message.text)
    if issuer is None:
        await message.answer(
            f"Слишком длинно — не больше {MAX_ISSUER_LENGTH} символов. Например: <i>{EXAMPLE_ISSUER}</i>"
        )
        return
    await finish_create(bot, message.from_user, state, issuer)


@staff.callback_query(StateFilter(CreateCert.issuer), F.data.startswith("issuer:"))
async def create_issuer_button(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    issuer = storage.last_issuer(call.from_user.id) if call.data == "issuer:last" else None
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=None)
    await finish_create(bot, call.from_user, state, issuer)


async def finish_create(bot: Bot, user: User, state: FSMContext, issuer: str | None) -> None:
    data = await state.get_data()
    await state.clear()
    if "code" not in data or "amount" not in data:
        await bot.send_message(user.id, "Что-то пошло не так — начните заново.", reply_markup=menu(user.id))
        return
    await issue(bot, user, data["code"], data["amount"], issuer)


async def issue(bot: Bot, user: User, code: str, amount: int, issuer: str | None) -> None:
    cert = storage.create_certificate(code, amount, user.id, user.full_name, issuer, now())
    if cert is None:
        await bot.send_message(
            user.id,
            f"Сертификат с номером <code>{code}</code> только что создал кто-то другой. "
            "Начните заново с другим номером.",
            reply_markup=menu(user.id),
        )
        return
    if issuer:
        storage.set_last_issuer(user.id, issuer)
    log.info("Issued %s for %s RUB by user %s", cert.code, cert.amount, user.id)

    def _render() -> tuple[bytes, bytes]:
        return to_jpeg(render_front(cert.amount)), to_jpeg(render_back(cert.code, cert.issued_by_name))

    front, back = await asyncio.to_thread(_render)
    await bot.send_media_group(user.id, media=[
        InputMediaPhoto(
            media=BufferedInputFile(front, filename=f"{cert.code}_front.jpg"),
            caption=f"🎟 <b>Подарочный сертификат</b>\n\n{cert_details(cert)}",
        ),
        InputMediaPhoto(media=BufferedInputFile(back, filename=f"{cert.code}_back.jpg")),
    ])
    await bot.send_message(
        user.id,
        f"✅ Сертификат <code>{cert.code}</code> создан.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📄 PDF для печати (2 стороны)", callback_data=f"pdf:{cert.code}"),
        ]]),
    )


@staff.callback_query(F.data.startswith("pdf:"))
async def send_pdf(call: CallbackQuery, bot: Bot) -> None:
    cert = storage.get(call.data.split(":", 1)[1])
    if cert is None:
        await call.answer("Сертификат не найден", show_alert=True)
        return
    await call.answer("Готовлю PDF…")
    pdf = await asyncio.to_thread(
        lambda: to_pdf([render_front(cert.amount), render_back(cert.code, cert.issued_by_name)])
    )
    await bot.send_document(
        call.from_user.id,
        BufferedInputFile(pdf, filename=f"certificate_{cert.code}.pdf"),
        caption=f"Сертификат № {h(cert.code)} — 2 страницы (лицевая сторона и оборот), 210×99 мм",
    )


# --- сотрудники (и администратор): проверка и погашение -------------------------------


@staff.message(Command("check"))
async def cmd_check(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    if command.args:
        await check_code(message, command.args)
    else:
        await check_prompt(message, state)


@staff.message(StateFilter(None), *TEXT_INPUT)
async def any_text(message: Message) -> None:
    await check_code(message, message.text)


@staff.message()
async def staff_other(message: Message) -> None:
    await message.answer(STAFF_HELP, reply_markup=menu(message.from_user.id))


@staff.callback_query()
async def staff_stale_button(call: CallbackQuery) -> None:
    await call.answer("Эта кнопка больше не действует")


def used_text(cert: Certificate) -> str:
    who = f"\nКто погасил: {h(cert.used_by_name)}" if cert.used_by_name else ""
    return (
        "⛔️ <b>Сертификат уже использован</b>\n\n"
        f"Номер: <code>{h(cert.code)}</code>\n"
        f"Сумма: {format_amount(cert.amount)}\n"
        f"Использован: {fmt_dt(cert.used_at)}{who}\n\n"
        "Принимать его нельзя."
    )


async def check_code(message: Message, text: str) -> None:
    code = normalize_code(text)
    if code is None:
        await message.answer(
            f"Номер сертификата — это {CODE_LENGTH} цифр, например <code>{EXAMPLE_CODE}</code>.",
            reply_markup=menu(message.from_user.id),
        )
        return
    cert = storage.get(code)
    if cert is None:
        await message.answer(f"❌ Сертификат <code>{code}</code> не найден.\nПроверьте, правильно ли введён номер.")
        return
    if cert.is_used:
        await message.answer(used_text(cert))
        return
    user = message.from_user
    if not storage.redeem(code, user.id, user.full_name, now()):
        # другой сотрудник погасил его в ту же секунду
        await message.answer(used_text(storage.get(code)))
        return
    log.info("Redeemed %s by user %s", code, user.id)
    await message.answer(
        f"✅ <b>Сертификат действителен</b>\n\n{cert_details(cert)}\n\n"
        "Сертификат отмечен как <b>использованный</b> — второй раз его принять не получится."
    )


# --- все остальные ----------------------------------------------------------------


@fallback.message()
async def no_access(message: Message) -> None:
    await message.answer("Нет доступа. Нажмите /start, чтобы отправить запрос администратору.")


@fallback.callback_query()
async def no_access_cb(call: CallbackQuery) -> None:
    await call.answer("Нет доступа", show_alert=True)


# --- запуск -------------------------------------------------------------------------


class ConflictHint(logging.Filter):
    """Ошибку Conflict объясняем по-русски (один раз), чтобы было понятно, что делать."""

    shown = False

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.shown and "TelegramConflictError" in record.getMessage():
            self.shown = True
            print(
                "\nВНИМАНИЕ: Telegram сообщает, что этот бот запущен ещё где-то — в другом окне "
                "или на другом компьютере. Пока работают две копии, бот будет отвечать с перебоями.\n"
                "Закройте лишнюю копию (или просто запустите start_bot.bat ещё раз на этом компьютере — "
                "старое окно закроется само).\n"
            )
        return True


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # чтобы русский текст не ронял бота при выводе в файл
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.dispatcher").addFilter(ConflictHint())
    if not config.BOT_TOKEN:
        print("Не задан токен бота. Откройте файл .env и впишите токен от @BotFather в строку BOT_TOKEN=")
        sys.exit(1)
    try:
        bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        me = await bot.get_me()
    except (TokenValidationError, TelegramUnauthorizedError):
        print("Токен бота неверный. Проверьте строку BOT_TOKEN= в файле .env (токен выдаёт @BotFather).")
        sys.exit(1)

    # Токен рабочий — значит, эта копия точно запустится; закрываем предыдущую, если она ещё открыта.
    try:
        stopped = await asyncio.to_thread(replace_previous_instance, config.BASE_DIR, config.DATA_DIR)
    except Exception as exc:  # проверка не должна мешать запуску
        log.warning("Could not check for a previous bot instance: %s", exc)
        stopped = 0
    if stopped:
        print("Предыдущая копия бота закрыта — теперь работает эта.")
        await asyncio.sleep(2)  # даём Telegram «отпустить» старое подключение

    dp = Dispatcher()
    dp.include_routers(common, admin, staff, fallback)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Что умеет бот"),
        BotCommand(command="cancel", description="Отменить действие"),
    ])

    print(f"Бот @{me.username} запущен. Не закрывайте это окно — иначе бот перестанет отвечать.")
    if not all_admin_ids():
        print("Администратор ещё не указан: напишите боту /start, он покажет ваш ID — впишите его в .env (ADMIN_IDS=).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
