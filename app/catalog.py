"""Категории и подкатегории каталога.

Категории берутся из прайса (config.MENU_CATEGORIES) в заданном порядке,
подкатегории — из фактических данных Google-таблицы. Так меню всегда
совпадает с тем, что реально загружено, без «пустых» пунктов.
"""
from __future__ import annotations

from hashlib import sha1

from .config import MENU_CATEGORIES

# Порядок групп в меню. Подкатегории не фиксируются: они приходят из прайса.
CATEGORY_STRUCTURE: dict[str, list[str]] = {name: [] for name in MENU_CATEGORIES}


def code(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()[:12]


def all_categories(db) -> list[str]:
    extra = [x for x in db.db_categories() if x not in CATEGORY_STRUCTURE]
    return list(CATEGORY_STRUCTURE) + extra


def category_from_code(db, cat_code: str) -> str | None:
    return next((cat for cat in all_categories(db) if code(cat) == cat_code), None)


def all_subcategories(db, category: str) -> list[str]:
    return db.db_subcategories(category)


def subcategory_from_code(db, category: str, sub_code: str) -> str | None:
    return next((sub for sub in all_subcategories(db, category) if code(sub) == sub_code), None)
