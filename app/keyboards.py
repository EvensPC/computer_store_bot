"""Клавиатуры каталога и админ-панели."""
from __future__ import annotations

from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .catalog import all_categories, all_subcategories, code
from .config import is_admin


def money(value: int | None) -> str:
    if value is None:
        return "не указана"
    whole, frac = divmod(value, 100)
    return f"{whole:,}".replace(",", " ") + (f",{frac:02d}" if frac else "") + " ₽"


def main_keyboard(db, user_id: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=cat)] for cat in all_categories(db)]
    rows.append([KeyboardButton(text="🔎 Поиск товара"), KeyboardButton(text="👤 Мой профиль")])
    rows.append([KeyboardButton(text="🤝 Стать оптовиком")])
    if is_admin(user_id):
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               is_persistent=True, input_field_placeholder="Выберите раздел")


def categories_keyboard(db, category: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for sub in all_subcategories(db, category):
        count = db.count_products(category, sub)
        b.button(text=f"{sub} · {count}", callback_data=f"sub:{code(category)}:{code(sub)}:0")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def products_keyboard(db, category: str, subcategory: str, page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    count = db.count_products(category, subcategory)
    if count:
        for product in db.products(category, subcategory, page):
            label = f"{product['name'][:39]}"
            b.button(text=label, callback_data=f"p:{product['id']}:{page}:l")
        if page > 0:
            b.button(text="⬅️ Назад", callback_data=f"sub:{code(category)}:{code(subcategory)}:{page-1}")
        if (page + 1) * 8 < count:
            b.button(text="Далее ➡️", callback_data=f"sub:{code(category)}:{code(subcategory)}:{page+1}")
    b.button(text="📂 Подкатегории", callback_data=f"cat:{code(category)}")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def product_keyboard(product: dict, page: int, source: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 Сравнить цены", callback_data=f"cmp:{product['id']}:{page}:{source}")
    dns_url = (product.get("dns_url") or "").strip()
    host = (urlsplit(dns_url).hostname or "").lower()
    if dns_url.startswith("https://") and (host == "dns-shop.ru" or host.endswith(".dns-shop.ru")):
        b.button(text="🖼 Товар на DNS", url=dns_url)
    if source == "s":
        b.button(text="⬅️ К поиску", callback_data="search:0")
    else:
        b.button(text="⬅️ К списку", callback_data=f"sub:{code(product['category'])}:{code(product['subcategory'])}:{page}")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def comparison_keyboard(product: dict, page: int, source: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К карточке", callback_data=f"p:{product['id']}:{page}:{source}")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def search_keyboard(results: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for product in results:
        b.button(text=product["name"][:43], callback_data=f"p:{product['id']}:{page}:s")
    if page > 0:
        b.button(text="⬅️ Назад", callback_data=f"search:{page-1}")
    if (page + 1) * 8 < total:
        b.button(text="Далее ➡️", callback_data=f"search:{page+1}")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def profile_keyboard(approved: bool, pending: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not approved and not pending:
        b.button(text="🤝 Запросить оптовый доступ", callback_data="apply")
    b.button(text="🏠 Главное меню", callback_data="home")
    b.adjust(1)
    return b.as_markup()


def admin_home_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, data in [
        ("🕘 Заявки на рассмотрении", "adm:q:0"),
        ("📋 История всех заявок", "adm:h:0"),
        ("✅ Список оптовиков", "adm:w:0"),
        ("📤 Выгрузить оптовиков (JSON)", "adm:export"),
        ("📥 Оптовики для Excel (CSV)", "adm:exportcsv"),
        ("🔄 Обновить Google-таблицу", "adm:reload"),
        ("☁️ Выгрузить оптовиков в таблицу", "adm:sheetpush"),
        ("🔎 Посмотреть столбцы прайса 379", "adm:headers"),
        ("🔎 Посмотреть начало прайса 482", "adm:otherheaders"),
        ("🏠 Главное меню", "home"),
    ]:
        b.button(text=text, callback_data=data)
    b.adjust(1)
    return b.as_markup()


def review_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm:approve:{request_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:reject:{request_id}")],
    ])
