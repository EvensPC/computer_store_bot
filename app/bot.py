"""Главная логика Telegram-бота (aiogram 3)."""
from __future__ import annotations

import asyncio
import logging
import signal
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .catalog import all_categories, category_from_code, subcategory_from_code
from .config import (ADMIN_IDS, BOT_TOKEN, DB_PATH, OTHER_PRICES_PATH,
                     PORT, PRODUCTS_PATH, SYNC_INTERVAL_MINUTES,
                     WEBHOOK_PATH, WEBHOOK_SECRET, WEBHOOK_URL,
                     WHOLESALERS_CSV_PATH, WHOLESALERS_PATH, is_admin)
from .database import Database
from .dns_photos import product_photo
from .sheet_sync import GoogleSheetSync
from .wholesale_sheet import WholesaleSheet
from .keyboards import (admin_home_keyboard, categories_keyboard,
                        comparison_keyboard, main_keyboard, money, product_keyboard,
                        products_keyboard, profile_keyboard, review_keyboard,
                        search_keyboard)

log = logging.getLogger(__name__)
router = Router()
db = Database(DB_PATH, WHOLESALERS_PATH)
sheet_sync = GoogleSheetSync(db)
wholesale_sheet = WholesaleSheet(db)
# Последний поисковый запрос хранится в памяти. Оптовые доступы и заявки — только на диске.
last_search: dict[int, str] = {}


class SearchState(StatesGroup):
    waiting = State()


def save_telegram_user(user):
    db.save_user(user.id, user.full_name, user.username or "")


def price_type(user_id: int) -> str:
    return "wholesale" if is_admin(user_id) or db.is_wholesaler(user_id) else "retail"


def safe_page(raw: str) -> int:
    page = int(raw)
    if not 0 <= page <= 10000:
        raise ValueError("invalid page")
    return page


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if not message.from_user:
        return
    save_telegram_user(message.from_user)
    await state.clear()
    await message.answer(
        "🖥 <b>Каталог компьютерных комплектующих</b>\n\n"
        "Выберите группу товаров в меню ниже. Внутри будут подгруппы и карточки "
        "с артикулами, ценами и сравнением с другим прайсом.",
        reply_markup=main_keyboard(db, message.from_user.id),
    )


@router.message(Command("id"))
async def my_id(message: Message):
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("help"))
async def help_menu(message: Message):
    await message.answer(
        "Команды: /start — меню; /id — Telegram ID; /admin — админ-панель; "
        "/cancel — отменить поиск.\n\n"
        "Товары ищутся по названию и артикулу. Запрос на оптовый доступ "
        "подтверждается администратором."
    )


@router.message(Command("cancel"))
async def cancel_search(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Поиск отменён.")


async def show_admin_home(message: Message, user_id: int):
    if not is_admin(user_id):
        await message.answer("⛔ Доступ только для администратора.")
        return
    pending, count = db.requests(pending_only=True)
    if wholesale_sheet.enabled:
        sheet_state = wholesale_sheet.last_success or "ещё не выгружались"
        if wholesale_sheet.last_error:
            sheet_state += f" | ошибка: {wholesale_sheet.last_error[:150]}"
        wholesale_line = f"\n☁️ Оптовики в Google-таблице: {escape(sheet_state)}"
    else:
        wholesale_line = "\n☁️ Google-таблица для оптовиков не настроена (WHOLESALE_SHEET_ID)"
    await message.answer(
        f"🛠 <b>Админ-панель</b>\nОжидают решения: <b>{count}</b>\n"
        f"Оптовиков в реестре: <b>{len(db.wholesalers())}</b>\n"
        f"Google Sheets: {escape(sheet_sync.last_success or 'ещё не синхронизирована')}"
        + wholesale_line
        + (f"\nПоследняя ошибка: {escape(sheet_sync.last_error[:250])}" if sheet_sync.last_error else ""),
        reply_markup=admin_home_keyboard(),
    )


@router.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user:
        await show_admin_home(message, message.from_user.id)


async def show_profile(message: Message, user_id: int):
    user = db.user(user_id)
    role = price_type(user_id)
    req = db.latest_request(user_id)
    approved = role == "wholesale"
    pending = bool(req and req["status"] == "pending") and not approved
    if is_admin(user_id):
        status = "Администратор (оптовые цены доступны)"
    elif approved:
        status = "✅ Оптовик"
    elif pending:
        status = "⏳ Заявка на оптовые цены рассматривается"
    elif req and req["status"] == "rejected":
        status = "Розничный покупатель (прошлая заявка отклонена)"
    else:
        status = "Розничный покупатель"
    await message.answer(
        f"👤 <b>Мой профиль</b>\n"
        f"Имя: {escape((user or {}).get('full_name') or '—')}\n"
        f"Telegram ID: <code>{user_id}</code>\nСтатус: {status}",
        reply_markup=profile_keyboard(approved, pending),
    )


async def apply_for_wholesale(message: Message, user):
    save_telegram_user(user)
    if is_admin(user.id) or db.is_wholesaler(user.id):
        await message.answer("✅ Оптовые цены уже доступны в вашем профиле.")
        return
    request, is_new = db.request_wholesale(user.id)
    if not is_new:
        await message.answer(f"⏳ Ваша заявка №{request['id']} уже ожидает решения администратора.")
        return
    asyncio.create_task(sync_wholesale_sheet())
    await message.answer(
        f"✅ Заявка №{request['id']} отправлена!\n"
        "После одобрения оптовые цены автоматически появятся в карточках товаров."
    )
    caption = (
        f"📨 <b>Новая заявка на оптовые цены №{request['id']}</b>\n"
        f"Покупатель: {escape(user.full_name)}\n"
        f"Username: @{escape(user.username) if user.username else 'не указан'}\n"
        f"ID: <code>{user.id}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(
                admin_id, caption, reply_markup=review_keyboard(request["id"])
            )
        except TelegramAPIError as exc:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, exc)


async def show_search_results(message: Message, user_id: int, page: int):
    term = last_search.get(user_id)
    if not term:
        await message.answer("Поисковый запрос не найден. Нажмите «🔎 Поиск товара» снова.")
        return
    results, count = db.search(term, page)
    if not results:
        await message.answer("По вашему запросу товары не найдены. Попробуйте другое название или артикул.")
        return
    await message.answer(
        f"🔎 <b>Поиск: {escape(term)}</b>\nНайдено товаров: {count} · Страница {page+1}",
        reply_markup=search_keyboard(results, page, count),
    )


@router.message(F.text)
async def text_menu(message: Message, state: FSMContext):
    if not message.from_user:
        return
    user = message.from_user
    text = (message.text or "").strip()
    save_telegram_user(user)
    if text in all_categories(db):
        await state.clear()
        await message.answer(
            f"📂 <b>{escape(text)}</b>\nВыберите подгруппу:",
            reply_markup=categories_keyboard(db, text),
        )
    elif text == "🔎 Поиск товара":
        await state.set_state(SearchState.waiting)
        await message.answer("Введите название товара или его артикул (например: DEMO-001).\n/cancel — отменить.")
    elif text == "👤 Мой профиль":
        await state.clear()
        await show_profile(message, user.id)
    elif text == "🤝 Стать оптовиком":
        await state.clear()
        await apply_for_wholesale(message, user)
    elif text == "🛠 Админ-панель":
        await state.clear()
        await show_admin_home(message, user.id)
    elif await state.get_state() == SearchState.waiting.state:
        if len(text) < 2 or len(text) > 100:
            await message.answer("Введите от 2 до 100 символов для поиска.")
            return
        last_search[user.id] = text
        await state.clear()
        await show_search_results(message, user.id, 0)
    else:
        await message.answer("Выберите раздел в меню или нажмите /start.")


@router.callback_query(F.data == "home")
async def callback_home(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.clear()
    if query.message:
        await query.message.answer(
            "🏠 <b>Главное меню</b> — выберите группу товаров:",
            reply_markup=main_keyboard(db, query.from_user.id),
        )


@router.callback_query(F.data == "apply")
async def callback_apply(query: CallbackQuery):
    await query.answer()
    if query.message:
        await apply_for_wholesale(query.message, query.from_user)


@router.callback_query(F.data.startswith("cat:"))
async def callback_category(query: CallbackQuery):
    cat = category_from_code(db, (query.data or "").removeprefix("cat:"))
    if not cat:
        await query.answer("Раздел не найден. Обновите меню.", show_alert=True)
        return
    await query.answer()
    if query.message:
        await query.message.answer(f"📂 <b>{escape(cat)}</b>\nВыберите подгруппу:",
                                   reply_markup=categories_keyboard(db, cat))


@router.callback_query(F.data.startswith("sub:"))
async def callback_subcategory(query: CallbackQuery):
    try:
        _, cat_code, sub_code, page_str = (query.data or "").split(":")
        page = safe_page(page_str)
        cat = category_from_code(db, cat_code)
        sub = subcategory_from_code(db, cat, sub_code) if cat else None
    except ValueError:
        cat = sub = None
        page = 0
    if not cat or not sub:
        await query.answer("Подкатегория не найдена. Обновите меню.", show_alert=True)
        return
    await query.answer()
    if query.message:
        count = db.count_products(cat, sub)
        await query.message.answer(
            f"📦 <b>{escape(cat)} / {escape(sub)}</b>\n"
            f"Товаров: {count}. Выберите товар:",
            reply_markup=products_keyboard(db, cat, sub, page),
        )


async def send_product(message: Message, product: dict, user_id: int, page: int, source: str):
    is_opt = price_type(user_id) == "wholesale"
    retail = money(product["retail_price"])
    wholesale = money(product["wholesale_price"])
    caption = (
        f"🛒 <b>{escape(product['name'])}</b>\n\n"
        f"📁 {escape(product['category'])} / {escape(product['subcategory'])}\n"
        f"🏷 Артикул: "
        + ("не указан в таблице" if product['sku'].startswith('_no_sku_')
           else f"<code>{escape(product['sku'])}</code>") + "\n\n"
    )
    if is_opt:
        caption += f"💼 <b>Оптовая цена: {wholesale}</b>\nРозничная цена: {retail}"
    else:
        caption += f"💳 <b>Розничная цена: {retail}</b>\n🔒 Оптовая цена — после одобрения заявки"
    keyboard = product_keyboard(product, page, source)
    photo = await product_photo(product)
    if photo:
        try:
            await message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
            return
        except TelegramAPIError as exc:
            log.info("Не удалось отправить фото SKU=%s: %s", product["sku"], exc)
    await message.answer(caption, reply_markup=keyboard)


@router.callback_query(F.data.startswith("p:"))
async def callback_product(query: CallbackQuery):
    try:
        _, product_id, page_str, source = (query.data or "").split(":")
        page = safe_page(page_str)
        if source not in ("l", "s"):
            raise ValueError
        product = db.product(int(product_id))
    except ValueError:
        product = None
        page, source = 0, "l"
    if not product:
        await query.answer("Товар не найден (возможно, прайс обновился).", show_alert=True)
        return
    await query.answer()
    if query.message:
        await send_product(query.message, product, query.from_user.id, page, source)


@router.callback_query(F.data.startswith("cmp:"))
async def callback_compare(query: CallbackQuery):
    try:
        _, product_id, page_str, source = (query.data or "").split(":")
        page = safe_page(page_str)
        if source not in ("l", "s"):
            raise ValueError
        product = db.product(int(product_id))
    except ValueError:
        product = None
        page, source = 0, "l"
    if not product:
        await query.answer("Товар не найден.", show_alert=True)
        return
    await query.answer()
    level = price_type(query.from_user.id)
    our_price = product["wholesale_price"] if level == "wholesale" else product["retail_price"]
    rows = db.comparisons(product["sku"], level)
    label = "оптовая" if level == "wholesale" else "розничная"
    lines = [
        f"📊 <b>Сравнение: {escape(product['name'])}</b>",
        f"Артикул: " + ("не указан" if product['sku'].startswith('_no_sku_')
                       else f"<code>{escape(product['sku'])}</code>"),
        f"Прайс 379 ({label} цена): <b>{money(our_price)}</b>", "",
    ]
    if our_price is None:
        lines.append("В вашем типе прайса собственная цена ещё не заполнена; разницу считать нельзя.")
    if not rows:
        lines.append("В прайсе 482 не найдено надёжного совпадения товара по артикулу или полному названию.")
    else:
        for item in rows[:15]:
            if our_price is None:
                difference = "сравнение недоступно без нашей цены"
            else:
                delta = our_price - item["price"]
                pct = abs(delta) / item["price"] * 100
                if delta > 0:
                    difference = f"наша цена выше на {money(delta)} ({pct:.1f}%)"
                elif delta < 0:
                    difference = f"наша цена ниже на {money(-delta)} ({pct:.1f}%)"
                else:
                    difference = "цены совпадают"
            lines.append(f"• {escape(item['supplier'])}: <b>{money(item['price'])}</b>\n  {difference}")
        if len(rows) > 15:
            lines.append(f"…и ещё {len(rows) - 15} предложений")
    lines.append("\nПрайс 482 — справочная цена из второй Google-таблицы; "
                 "её уровень (опт/розница) не подтверждён. Для оптовика сравнивается "
                 "его цена 379 с этой справочной ценой. Совпадение — по артикулу "
                 "или одинаковому полному названию.")
    if query.message:
        await query.message.answer("\n".join(lines),
                                   reply_markup=comparison_keyboard(product, page, source))


@router.callback_query(F.data.startswith("search:"))
async def callback_search(query: CallbackQuery):
    try:
        page = safe_page((query.data or "").split(":")[1])
    except (ValueError, IndexError):
        await query.answer("Некорректная страница поиска.", show_alert=True)
        return
    await query.answer()
    if query.message:
        await show_search_results(query.message, query.from_user.id, page)


async def show_requests(message: Message, pending_only: bool, page: int):
    rows, total = db.requests(page=page, pending_only=pending_only)
    title = "🕘 <b>Новые заявки</b>" if pending_only else "📋 <b>История заявок</b>"
    lines = [f"{title} · всего: {total} · страница {page+1}"]
    b = InlineKeyboardBuilder()
    statuses = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    for item in rows:
        name = escape(item.get("full_name") or "Без имени")
        lines.append(
            f"\n{statuses.get(item['status'], '•')} <b>№{item['id']}</b> "
            f"{name} · <code>{item['telegram_id']}</code>\n"
            f"{item['created_at']} · {item['status']}"
        )
        if item["status"] == "pending":
            b.button(text=f"✅ Одобрить №{item['id']}", callback_data=f"adm:approve:{item['id']}")
            b.button(text=f"❌ Отклонить №{item['id']}", callback_data=f"adm:reject:{item['id']}")
    if not rows:
        lines.append("\nЗаявок на этой странице нет.")
    prefix = "q" if pending_only else "h"
    if page:
        b.button(text="⬅️ Предыдущая", callback_data=f"adm:{prefix}:{page-1}")
    if (page+1)*5 < total:
        b.button(text="Следующая ➡️", callback_data=f"adm:{prefix}:{page+1}")
    b.button(text="🛠 Админ-панель", callback_data="adm:home")
    b.adjust(1)
    await message.answer("\n".join(lines), reply_markup=b.as_markup())


async def show_wholesalers(message: Message, page: int):
    approved = db.wholesalers()
    b = InlineKeyboardBuilder()
    lines = [f"✅ <b>Оптовики</b> · всего: {len(approved)} · страница {page+1}"]
    for user in approved[page*5: (page+1)*5]:
        user_id = int(user["telegram_id"])
        name = escape(user.get("full_name") or "Без имени")
        username = f"@{escape(user['username'])}" if user.get("username") else "без username"
        lines.append(f"\n• {name} ({username})\n  ID: <code>{user_id}</code>\n  Доступ с: {user.get('approved_at', '—')}")
        b.button(text=f"🚫 Отозвать доступ: {user_id}", callback_data=f"adm:drop:{user_id}")
    if not approved[page*5:(page+1)*5]:
        lines.append("\nВ реестре нет пользователей на этой странице.")
    if page:
        b.button(text="⬅️ Предыдущая", callback_data=f"adm:w:{page-1}")
    if (page+1)*5 < len(approved):
        b.button(text="Следующая ➡️", callback_data=f"adm:w:{page+1}")
    b.button(text="🛠 Админ-панель", callback_data="adm:home")
    b.adjust(1)
    await message.answer("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("adm:"))
async def callback_admin(query: CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Нет прав администратора.", show_alert=True)
        return
    if not query.message:
        await query.answer()
        return
    pieces = (query.data or "").split(":")
    action = pieces[1] if len(pieces) > 1 else ""
    await query.answer()
    if action == "home":
        await show_admin_home(query.message, query.from_user.id)
        return
    if action in ("q", "h", "w"):
        try:
            page = safe_page(pieces[2])
        except (IndexError, ValueError):
            await query.message.answer("Некорректная страница.")
            return
        if action == "w":
            await show_wholesalers(query.message, page)
        else:
            await show_requests(query.message, action == "q", page)
        return
    if action in ("approve", "reject"):
        try:
            request_id = int(pieces[2])
        except (IndexError, ValueError):
            await query.message.answer("Некорректный номер заявки.")
            return
        row = db.review_request(request_id, action == "approve", query.from_user.id)
        if row is None:
            await query.message.answer("Эта заявка уже обработана или не существует.")
            return
        asyncio.create_task(sync_wholesale_sheet())
        approved = action == "approve"
        await query.message.answer(
            f"{'✅ Одобрена' if approved else '❌ Отклонена'} заявка №{request_id} "
            f"(ID пользователя: <code>{row['telegram_id']}</code>)."
        )
        try:
            text = ("✅ Ваша заявка на оптовые цены одобрена! Перейдите в карточку товара "
                    "— теперь вы увидите оптовую цену." if approved else
                    "❌ Заявка на оптовые цены отклонена администратором.")
            await query.bot.send_message(row["telegram_id"], text)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить решение покупателю %s: %s", row["telegram_id"], exc)
        return
    if action == "drop":
        try:
            user_id = int(pieces[2])
        except (IndexError, ValueError):
            await query.message.answer("Некорректный ID.")
            return
        removed = db.remove_wholesaler(user_id, query.from_user.id)
        await query.message.answer("✅ Оптовый доступ отозван." if removed else "Этот пользователь уже исключён из списка.")
        if removed:
            asyncio.create_task(sync_wholesale_sheet())
            try:
                await query.bot.send_message(user_id, "Ваш оптовый доступ был отозван администратором.")
            except TelegramAPIError:
                pass
        return
    if action == "sheetpush":
        if not wholesale_sheet.enabled:
            await query.message.answer(
                "☁️ Google-таблица для оптовиков не настроена.\n"
                "Задайте WHOLESALE_SHEET_ID и GOOGLE_SERVICE_ACCOUNT_FILE в .env."
            )
            return
        try:
            active, total = await asyncio.to_thread(wholesale_sheet.push)
        except Exception as exc:
            await query.message.answer(f"❌ Не удалось выгрузить: {escape(str(exc)[:300])}")
            return
        await query.message.answer(
            f"☁️ Выгружено в Google-таблицу: оптовиков {active}, заявок {total}."
        )
        return
    if action == "export":
        await query.message.answer_document(
            document=FSInputFile(WHOLESALERS_PATH),
            caption="📤 Отдельный постоянный реестр оптовиков (JSON). Храните резервную копию в защищённом месте.",
        )
        return
    if action == "exportcsv":
        db.export_wholesalers_csv(WHOLESALERS_CSV_PATH)
        await query.message.answer_document(
            document=FSInputFile(WHOLESALERS_CSV_PATH),
            caption="📥 Экспорт оптовиков в CSV для Excel. Не публикуйте файл с личными данными.",
        )
        return
    if action == "headers":
        try:
            headers = await asyncio.to_thread(sheet_sync.headers)
            await query.message.answer(
                "📊 <b>Заголовки Google-таблицы:</b>\n" +
                "\n".join(f"{i+1}. {escape(head or '(пусто)')}" for i, head in enumerate(headers[:35])) +
                "\n\nНастройка соответствия колонок: data/column_map.json"
            )
        except Exception as exc:
            await query.message.answer(f"❌ Не удалось прочитать заголовки: {escape(str(exc))[:700]}")
        return
    if action == "otherheaders":
        try:
            rows = await asyncio.to_thread(sheet_sync.preview_other)
            preview = ["🔎 <b>Первые строки прайса 482</b> (столбцы A, B, C ...):"]
            for number, row in enumerate(rows[:10], start=1):
                fields = [f"{chr(65+i)}: {escape(value[:65])}" for i, value in enumerate(row[:6]) if value.strip()]
                preview.append(f"\n<b>Строка {number}</b> " + " | ".join(fields))
            preview.append("\nЕсли автоопределение цены ошиблось: настройте other_columns "
                           "в data/column_map.json (см. README).")
            await query.message.answer("\n".join(preview)[:3900])
        except Exception as exc:
            await query.message.answer(f"❌ Не удалось прочитать прайс 482: {escape(str(exc))[:700]}")
        return
    if action == "reload":
        try:
            product_count, other_count, warnings = await asyncio.to_thread(sheet_sync.refresh)
        except Exception as exc:
            log.exception("Обновление Google Sheets не удалось")
            await query.message.answer(f"❌ Ошибка импорта: {escape(str(exc))[:750]}\nПредыдущие цены сохранены.")
        else:
            suffix = ("\n⚠️ Пропущены некоторые строки: " + escape("; ".join(warnings[:3]))[:500]) if warnings else ""
            await query.message.answer(
                f"✅ Google-таблица загружена. Товаров: {product_count}; цен второго прайса: {other_count}.\n"
                "Права оптовиков и заявки не затронуты." + suffix
            )
        return
    await query.message.answer("Команда администратора не найдена.")


@router.callback_query()
async def other_callback(query: CallbackQuery):
    await query.answer("Кнопка устарела. Откройте /start.", show_alert=True)


async def periodic_sync():
    """Периодически подтягивает изменения таблицы, не останавливая бот при сетевой ошибке."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL_MINUTES * 60)
        try:
            count, other, warnings = await asyncio.to_thread(sheet_sync.refresh)
            log.info("Google Sheets обновлена: %d товаров, %d цен сравнения", count, other)
        except Exception as exc:
            log.warning("Плановая синхронизация не удалась (старый каталог сохранён): %s", exc)
        await sync_wholesale_sheet()


async def sync_wholesale_sheet():
    """Выгрузка оптовиков и заявок в Google-таблицу. Не блокирует ответ пользователю."""
    if not wholesale_sheet.enabled:
        return
    try:
        await asyncio.to_thread(wholesale_sheet.push)
    except Exception as exc:
        log.warning("Не удалось записать оптовиков и заявки в Google-таблицу: %s", exc)


async def restore_wholesale_state():
    """При старте подтягивает оптовиков и заявки из Google-таблицы (восстановление после передеплоя)."""
    if not wholesale_sheet.enabled:
        return
    try:
        active, requests = await asyncio.to_thread(wholesale_sheet.restore_and_push)
        log.info("Восстановление из таблицы завершено: %d оптовиков, %d заявок", active, requests)
    except Exception as exc:
        log.warning("Не удалось восстановить оптовиков из Google-таблицы: %s", exc)


async def _initial_sync():
    """Первая загрузка Google Sheets в фоне, чтобы не задерживать старт сервера."""
    try:
        count, other, warnings = await asyncio.to_thread(sheet_sync.refresh)
        log.info("Google Sheets: %d товаров; %d цен сравнения", count, other)
    except Exception as exc:
        log.warning("Google Sheets недоступна. Предыдущие цены сохранены: %s", exc)


async def _run_polling(bot: Bot, dp: Dispatcher):
    """Режим long polling: VPS, локальный запуск, Background Worker."""
    sync_task = asyncio.create_task(_initial_sync())
    periodic_task = asyncio.create_task(periodic_sync())
    restore_task = asyncio.create_task(restore_wholesale_state())
    try:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            log.warning("Не удалось снять webhook перед polling: %s", exc)
        await dp.start_polling(bot)
    finally:
        for task in (sync_task, periodic_task, restore_task):
            task.cancel()
        await asyncio.gather(sync_task, periodic_task, restore_task, return_exceptions=True)


async def _run_webhook(bot: Bot, dp: Dispatcher):
    """Режим webhook: нужен для Render Web Service, который требует открытый PORT."""
    from aiohttp import web

    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    webhook_full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    sync_task = asyncio.create_task(_initial_sync())
    periodic_task = asyncio.create_task(periodic_sync())
    restore_task = asyncio.create_task(restore_wholesale_state())

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "computer_store_bot"})

    async def status(request: web.Request) -> web.Response:
        """Диагностика без секретов: видно, что env подхватились."""
        return web.json_response({
            "bot_token_set": bool(BOT_TOKEN),
            "admins": len(ADMIN_IDS),
            "webhook_mode": True,
            "port": PORT,
            "webhook_url": webhook_full_url,
            "products": db.count_all_products(),
            "wholesalers": len(db.wholesalers()),
            "sheet_last_success": sheet_sync.last_success,
            "sheet_last_error": sheet_sync.last_error,
            "wholesale_sheet_enabled": wholesale_sheet.enabled,
            "wholesale_sheet_last_error": wholesale_sheet.last_error,
        })

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None,
    ).register(app, path=WEBHOOK_PATH)
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/status", status)

    setup_application(app, dp, bot=bot)

    async def install_webhook() -> None:
        """Ставит webhook с повторами: падение Telegram не должно убивать контейнер."""
        delay = 5
        while True:
            try:
                await bot.set_webhook(
                    webhook_full_url,
                    secret_token=WEBHOOK_SECRET or None,
                    drop_pending_updates=False,
                    allowed_updates=dp.resolve_used_update_types(),
                )
                log.info("Webhook установлен: %s", webhook_full_url)
                return
            except Exception as exc:
                log.error("Не удалось установить webhook (%s). Повтор через %s с", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP-сервер слушает 0.0.0.0:%s", PORT)
    webhook_task = asyncio.create_task(install_webhook())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows и некоторые окружения не поддерживают add_signal_handler.
            pass

    try:
        await stop_event.wait()
    finally:
        log.info("Останавливаю webhook-сервер")
        webhook_task.cancel()
        for task in (sync_task, periodic_task, restore_task, webhook_task):
            task.cancel()
        await asyncio.gather(sync_task, periodic_task, restore_task, webhook_task,
                             return_exceptions=True)
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:  # сеть может уже отсутствовать при остановке
            log.warning("Не удалось снять webhook: %s", exc)
        await runner.cleanup()


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_TOKEN_FROM_BOTFATHER":
        raise RuntimeError(
            "Не задан BOT_TOKEN.\n"
            "  Render: Dashboard -> ваш сервис -> Environment -> Add Environment Variable\n"
            "          BOT_TOKEN = токен от @BotFather, затем Save (сервис перезапустится).\n"
            "  Локально: скопируйте .env.example в .env и укажите BOT_TOKEN там.\n"
            "  Подробнее: README.md, раздел 1."
        )
    if not ADMIN_IDS:
        raise RuntimeError(
            "Не задан ADMIN_IDS.\n"
            "  Render: Environment -> Add Environment Variable\n"
            "          ADMIN_IDS = ваш числовой Telegram ID (например 855724126).\n"
            "  Локально: укажите ADMIN_IDS в .env.\n"
            "  Подробнее: README.md, раздел 1."
        )
    # Если интернет временно отсутствует, используем последний сохранённый каталог.
    if PRODUCTS_PATH.exists() and OTHER_PRICES_PATH.exists():
        try:
            count, other = db.import_prices(PRODUCTS_PATH, OTHER_PRICES_PATH)
            log.info("Восстановлен каталог с диска: %d товаров", count)
        except (ValueError, OSError) as exc:
            log.error("Ошибка локальной копии прайса: %s", exc)
    # Свежие цены подтягиваются в фоне из _run_webhook/_run_polling,
    # чтобы Render успел увидеть открытый порт и пройти health check.
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        if WEBHOOK_URL:
            await _run_webhook(bot, dp)
        else:
            await _run_polling(bot, dp)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
