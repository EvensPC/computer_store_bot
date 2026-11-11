"""Порядок разделов главного меню и подкатегорий."""
from __future__ import annotations

from hashlib import sha1

# Порядок скопирован с предоставленного примера. Можно менять под собственный каталог.
CATEGORY_STRUCTURE: dict[str, list[str]] = {
    "Блоки питания": ["ATX", "SFX"],
    "Видеокарты": ["NVIDIA", "AMD"],
    "Жесткие диски и SSD": ["SSD", "HDD"],
    "Материнские платы": ["Intel", "AMD"],
    "Оперативная память": ["DDR4", "DDR5"],
    "Процессоры": ["Intel", "AMD"],
    "Системы охлаждения": ["Воздушное", "Жидкостное"],
    "Корпуса": ["Midi-Tower", "Mini-Tower"],
    "Комплектующие Б/у": ["Видеокарты Б/у", "Процессоры Б/у"],
    "Мониторы": ["24 дюйма", "27 дюймов"],
    "Периферия": ["Клавиатуры", "Мыши"],
    "Накопители данных": ["Флешки", "Внешние SSD"],
    "Компьютеры и ноутбуки": ["Системные блоки", "Ноутбуки"],
    "Разное": ["Кабели", "Адаптеры и хабы"],
}


def code(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()[:12]


def all_categories(db) -> list[str]:
    extra = [x for x in db.db_categories() if x not in CATEGORY_STRUCTURE]
    return list(CATEGORY_STRUCTURE) + extra


def category_from_code(db, cat_code: str) -> str | None:
    return next((cat for cat in all_categories(db) if code(cat) == cat_code), None)


def all_subcategories(db, category: str) -> list[str]:
    default = CATEGORY_STRUCTURE.get(category, [])
    extra = [x for x in db.db_subcategories(category) if x not in default]
    return [*default, *extra]


def subcategory_from_code(db, category: str, sub_code: str) -> str | None:
    return next((sub for sub in all_subcategories(db, category) if code(sub) == sub_code), None)
