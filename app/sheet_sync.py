"""Импорт товаров из указанной Google-таблицы, с поддержкой приватного доступа.

Можно читать открытый лист через CSV export или приватный через Google service account.
Колонки сопоставляются автоматически либо с помощью data/column_map.json.
Синхронизация выполняется транзакционно и не затрагивает права оптовиков.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from statistics import median
from pathlib import Path
from threading import RLock
from urllib.parse import quote

import requests

from . import config
from .database import Database, price_number

log = logging.getLogger(__name__)

# Каноническое имя => возможные заголовки таблицы. Их можно переопределить в JSON.
ALIASES = {
    "sku": ["артикул", "арт", "арт.", "код товара", "код", "sku", "part number", "partnumber", "vendor code", "p/n", "pn"],
    "name": ["наименование", "наименование товара", "название", "название товара", "товар", "модель", "номенклатура", "описание", "name", "product"],
    "category": ["категория", "категория товара", "группа", "группа товаров", "раздел", "category", "group"],
    "subcategory": ["подкатегория", "подгруппа", "тип товара", "подраздел", "subcategory", "subgroup"],
    "retail_price": ["розница", "розничная цена", "цена розница", "цена розничная", "цена ррц", "ррц", "цена продажи", "цена", "стоимость", "price", "retail_price"],
    "wholesale_price": ["опт", "оптовая цена", "цена опт", "цена оптовая", "оптовая", "цена для оптовиков", "дилерская цена", "wholesale_price"],
    "dns_url": ["dns", "ссылка dns", "ссылка на dns", "dns_url", "ссылка днс", "ссылка на днс"],
    "photo_url": ["фото", "ссылка на фото", "изображение", "картинка", "photo_url", "image_url", "photo"],
    "competitor_price": ["цена конкурента", "чужая цена", "второй прайс", "цена другого прайса", "competitor_price"],
    "competitor_supplier": ["конкурент", "поставщик сравнения", "название другого прайса", "competitor_supplier"],
    "competitor_url": ["ссылка конкурента", "url конкурента", "competitor_url"],
    "price": ["цена", "стоимость", "price", "цена конкурента"],
    "supplier": ["поставщик", "конкурент", "магазин", "supplier", "source"],
    "price_type": ["тип цены", "тип прайса", "price_type", "уровень цен"],
    "url": ["ссылка", "url", "ссылка на товар", "product_url"],
}

PRODUCT_FIELDS = ("sku", "name", "category", "subcategory", "retail_price", "wholesale_price", "dns_url", "photo_url", "competitor_price", "competitor_supplier", "competitor_url")
OTHER_FIELDS = ("sku", "supplier", "price_type", "price", "url", "name")


def normalize(text: str) -> str:
    text = str(text).casefold().replace("ё", "е").replace("_", " ")
    return " ".join(re.sub(r"[^\w\s]+", " ", text).split())


def column_index(spec: object, headers: list[str]) -> int | None:
    """Поддерживает название шапки, букву столбца (A/AA) или 1-based номер."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int):
        return spec - 1 if 1 <= spec <= len(headers) else None
    value = str(spec).strip()
    if value.startswith("#") and value[1:].isdigit():
        return int(value[1:]) - 1
    exact = {normalize(h): i for i, h in enumerate(headers) if h.strip()}
    if normalize(value) in exact:
        return exact[normalize(value)]
    if re.fullmatch(r"[A-Z]{1,3}", value) and value not in {h.strip() for h in headers}:
        result = 0
        for char in value:
            result = result * 26 + ord(char) - 64
        return result - 1
    return None


def resolve_columns(headers: list[str], names: tuple[str, ...], overrides: dict) -> dict[str, int | None]:
    normalized = {normalize(header): i for i, header in enumerate(headers) if header.strip()}
    resolved = {}
    for name in names:
        if name in overrides:
            resolved[name] = column_index(overrides[name], headers)
        else:
            resolved[name] = next((normalized[normalize(alias)] for alias in ALIASES.get(name, [])
                                   if normalize(alias) in normalized), None)
    return resolved


def cell(row: list[str], index: int | None) -> str:
    return row[index].strip() if index is not None and 0 <= index < len(row) else ""


def category_guess(name: str) -> str:
    """Используется лишь если в источнике нет колонки категории."""
    t = normalize(name)
    rules = [
        ("Комплектующие Б/у", r"\b(б у|бу|used|б/у)\b"),
        ("Видеокарты", r"\b(rtx|gtx|radeon|geforce|видеокарта|видеоускоритель|rx\s?\d{4})\b"),
        ("Материнские платы", r"\b(материнск|motherboard|b650|b760|z790|h610|x670|x870)"),
        ("Оперативная память", r"\b(ddr3|ddr4|ddr5|озу|оперативн|sodimm|so dimm)"),
        ("Процессоры", r"\b(процессор|cpu|ryzen|xeon|core i[3579]|core ultra)"),
        ("Блоки питания", r"\b(блок питания|питания atx|power supply|psu)"),
        ("Жесткие диски и SSD", r"\b(ssd|hdd|nvme|m 2|жесткий диск|твердотельн)"),
        ("Системы охлаждения", r"\b(охлаждени|кулер|cooler|вентилятор|радиатор|aio|водянк)"),
        ("Мониторы", r"\b(монитор|display|monitor)"),
        ("Периферия", r"\b(клавиатура|мышь|гарнитура|наушник|mouse|keyboard|webcam)"),
        ("Накопители данных", r"\b(флешк|usb накопитель|карта памяти|micro sd)"),
        ("Компьютеры и ноутбуки", r"\b(ноутбук|ноут|laptop|системный блок|готовый пк|мини пк)"),
        ("Корпуса", r"\b(корпус|chassis|case\b)"),
    ]
    for category, pattern in rules:
        if re.search(pattern, t):
            return category
    return "Разное"


def subcategory_guess(category: str, name: str) -> str:
    t = normalize(name)
    if category == "Видеокарты":
        return "AMD" if re.search(r"\b(radeon|rx\s?\d{4})\b", t) else "NVIDIA" if re.search(r"\b(rtx|gtx|geforce)\b", t) else "Другие"
    if category in ("Процессоры", "Материнские платы"):
        return "AMD" if "amd" in t or "ryzen" in t or "am5" in t else "Intel" if "intel" in t or "core i" in t or "lga" in t else "Другие"
    if category == "Оперативная память":
        return next((d.upper() for d in ("ddr5", "ddr4", "ddr3") if d in t), "Другие")
    if category == "Жесткие диски и SSD":
        return "SSD" if "ssd" in t or "nvme" in t else "HDD" if "hdd" in t else "Другие"
    if category == "Мониторы":
        m = re.search(r"\b(2[347]|3[124])\s?(?:дюйм|\"|inch)", t)
        return f"{m.group(1)} дюймов" if m else "Другие"
    if category == "Периферия":
        return "Клавиатуры" if "клавиат" in t or "keyboard" in t else "Мыши" if "мыш" in t or "mouse" in t else "Другие"
    if category == "Компьютеры и ноутбуки":
        return "Ноутбуки" if "ноут" in t or "laptop" in t else "Системные блоки"
    return "Другие товары"


def read_mapping(path: Path) -> dict:
    if not path.exists():
        return {"header_row": 1, "columns": {}}
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(obj, dict) or not isinstance(obj.get("columns", {}), dict):
        raise ValueError("column_map.json: ожидается JSON-объект с объектом columns")
    row = int(obj.get("header_row", 1))
    if row < 1 or row > 50:
        raise ValueError("column_map.json: header_row должен быть от 1 до 50")
    return obj


def infer_legacy_columns(rows: list[list[str]]) -> dict[str, int] | None:
    """Для прайсов без шапки: A=артикул, B=название, C/D/...=цена.

    Ценовую колонку выбираем по числу валидных цен и их медиане, чтобы
    при наличии числовой колонки «остаток» предпочесть денежные значения.
    При неоднозначности ручная привязка колонок безопаснее автодогадки.
    """
    eligible = []
    for row in rows[:500]:
        if len(row) < 3:
            continue
        sku, name = row[0].strip(), row[1].strip()
        if not sku or len(sku) > 64 or not any(ch.isdigit() for ch in sku):
            continue
        if len(name) < 8 or not any(ch.isalpha() for ch in name):
            continue
        eligible.append(row)
    if len(eligible) < (2 if len(rows) >= 5 else 1):
        return None
    prices = []
    for i in range(2, min(max(map(len, eligible)), 12)):
        amounts = []
        for row in eligible:
            if i >= len(row):
                continue
            try:
                amounts.append(price_number(row[i]))
            except ValueError:
                pass
        if amounts and len(amounts) >= max(2, int(len(eligible) * 0.45)):
            prices.append((i, len(amounts), median(amounts)))
    if not prices:
        return None
    # Чаще заполненная колонка важнее, затем денежный масштаб и левый столбец.
    index, count, typical_price = max(prices, key=lambda p: (p[1], p[2], -p[0]))
    log.info("Автоопределение прайса без заголовков: A=артикул, B=название, %s=цена (по %s строкам)",
             chr(65 + index) if index < 26 else f"#{index+1}", count)
    return {"sku": 0, "name": 1, "price": index}


SKU_LIKE = re.compile(r"^\d[\d\-/.]{2,}$")


def _is_price_cell(text: str) -> bool:
    try:
        price_number(text)
        return True
    except ValueError:
        return False


def detect_columns(rows: list[list[str]], sample: int = 400) -> dict | None:
    """Определяет колонки по содержимому, когда шапка нестандартная.

    Нужно для прайса 379: артикул в B, название в C, цена в D, а шапка —
    текст вида «Код Блоки питания ...» / «Опт цена». Если шапка распознаётся
    обычным способом, эта функция не используется.
    """
    window = [row for row in rows[:sample] if row]
    if len(window) < 3:
        return None
    width = max((len(row) for row in window), default=0)
    if width < 3:
        return None
    stats = [{"sku": 0, "name": 0, "price": 0, "filled": 0} for _ in range(width)]
    for row in window:
        for i in range(min(width, len(row))):
            value = row[i].strip()
            if not value:
                continue
            stats[i]["filled"] += 1
            is_price = _is_price_cell(value)
            is_sku = bool(SKU_LIKE.match(value)) and len(value) <= 64
            if is_price:
                # Число может быть и артикулом (482), и ценой (379). Считаем
                # оба варианта, а колонку выбираем по разнице — см. ниже.
                stats[i]["price"] += 1
            if is_sku and not is_price:
                stats[i]["sku"] += 1
            elif not is_sku and len(value) >= 8 and sum(ch.isalpha() for ch in value) >= 3:
                stats[i]["name"] += 1
    minimum = max(3, int(len(window) * 0.2))

    # Колонка артикулов: где максимум «похожих на артикул», но минимум цен.
    # Иначе чисто цифровая колонка цен победила бы колонку артикулов.
    sku = max(range(width), key=lambda i: (stats[i]["sku"] - stats[i]["price"], -i))
    if stats[sku]["sku"] < minimum:
        return None

    name_candidates = [i for i in range(width) if i != sku]
    name = max(name_candidates, key=lambda i: stats[i]["name"], default=None)
    if name is None or stats[name]["name"] < minimum:
        return None

    prices = [i for i in range(width)
              if i not in (sku, name) and stats[i]["price"] >= minimum]
    if not prices:
        return None
    prices.sort()
    return {"sku": sku, "name": name, "prices": prices[:2]}


def apply_section(text: str, state: dict) -> None:
    """Разбирает строку-разделитель прайса: категория это или подкатегория."""
    raw = str(text or "").strip()
    if not raw:
        return
    low = normalize(raw)
    if low.startswith("код "):
        # Начало прайса: «Код Блоки питания 1000-2200W» или
        # «Код Комплектующие для ПК Блоки питания 1000-2200W».
        # Ищем категорию в любом месте заголовка, остаток — подкатегория.
        rest = raw[4:].strip()
        rest_fold = rest.casefold().replace("ё", "е")
        best = None
        for category in config.MENU_CATEGORIES:
            needle = category.casefold().replace("ё", "е")
            pos = rest_fold.find(needle)
            while pos >= 0:
                end = pos + len(needle)
                left_ok = pos == 0 or not rest_fold[pos - 1].isalnum()
                right_ok = end == len(rest_fold) or not rest_fold[end].isalnum()
                if left_ok and right_ok:
                    break
                pos = rest_fold.find(needle, pos + 1)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, category, end)
        if best:
            _, category, end = best
            state["category"] = category
            state["subcategory"] = rest[end:].strip(" -–—:,")
            return
    for category in config.MENU_CATEGORIES:
        if low == normalize(category):
            state["category"] = category
            state["subcategory"] = ""
            return
    # Всё остальное — подкатегория текущей группы.
    if state.get("category"):
        state["subcategory"] = raw


_UNIT_RE = re.compile(r"\b(\d+)\s*(gb|tb|mb|w|vt|mhz|ghz|mm)\b")
_GPU_ONE_RE = re.compile(r"\b(rtx|gtx|rx)\s*(\d{3,4})\s*(ti|super|xt|gre)\b")
_GPU_TWO_RE = re.compile(r"\b(rtx|gtx|rx)\s+(\d{3,4})\b")


def model_tokens(name: str) -> set[str]:
    """Слова названия для строгого сопоставления двух прайсов.

    Приводит «RTX 5060 Ti» → «rtx5060ti», «16 GB» → «16gb», чтобы одинаковые
    модели из разных таблиц сравнивались как равные. Односимвольные части
    моделей (B760M-A / B760M-F) сохраняются: они различают варианты.
    """
    text = str(name or "").casefold().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text)
    text = _UNIT_RE.sub(r"\1\2", text)
    text = _GPU_ONE_RE.sub(r"\1\2\3", text)
    text = _GPU_TWO_RE.sub(r"\1\2", text)
    return {token for token in text.split() if token}


def link_competitors(products: list[dict], competitors: list[dict]) -> tuple[list[dict], int, int]:
    """Сопоставление 379 ↔ 482: уникальный SKU, точное название, затем модель.

    Разные поставщики могут использовать несвязанные внутренние артикулы.
    Названия сравниваются по вложенности слов: все значимые слова строки 482
    должны присутствовать в названии товара 379 (с учётом «RTX 5060 Ti»).
    Так RTX 3050 не совпадёт с RTX 5060Ti, а B760M-A — с B760M-F.
    """
    def sku_key(value: str) -> str:
        return re.sub(r"\s+", "", str(value)).casefold()

    by_sku: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for product in products:
        if not product["sku"].startswith("_no_sku_"):
            by_sku.setdefault(sku_key(product["sku"]), []).append(product)
        by_name.setdefault(normalize(product["name"]), []).append(product)

    result: list[dict] = []
    matched_sku = matched_name = 0
    used = set()
    for other in competitors:
        external_sku = other.get("sku", "")
        external_name = other.get("name", "")
        item = None
        method = ""
        sku_matches = by_sku.get(sku_key(external_sku), []) if external_sku else []
        name_conflict = False
        if len(sku_matches) == 1:
            item, method = sku_matches[0], "sku"
            # Артикул считается главным ключом, но различающиеся наименования
            # явно помечаем: коды поставщиков могут не быть универсальными.
            name_conflict = bool(external_name and
                                 normalize(external_name) != normalize(item["name"]))
        if item is None and external_name:
            candidates = by_name.get(normalize(external_name), [])
            if len(candidates) == 1:
                item, method = candidates[0], "name"
        if item is None:
            continue
        converted = other.copy()
        converted["sku"] = item["sku"]
        source_name = other.get("supplier") or "Прайс 482"
        if not source_name.startswith("Прайс 482"):
            source_name = f"Прайс 482 / {source_name}"
        converted["supplier"] = source_name + (
            " (совпадение по названию)" if method == "name" else
            " (проверьте название!)" if name_conflict else "")
        key = (converted["sku"], converted["supplier"], converted["price_type"])
        if key in used:
            continue
        used.add(key)
        result.append(converted)
        matched_sku += method == "sku"
        matched_name += method == "name"

    # Третий проход: сопоставление по модели. Нужен, потому что поставщики
    # пишут названия по-разному («…, 80 PLUS Gold, белый (HA-850BA4-WH)» против
    # «… HA-850BA4-WH») и артикулы в таблицах не совпадают.
    linked_skus = {row["sku"] for row in result}
    product_tokens: list[tuple[dict, set[str]]] = []
    token_index: dict[str, set[int]] = {}
    for product in products:
        tokens = model_tokens(product["name"])
        index = len(product_tokens)
        product_tokens.append((product, tokens))
        for token in tokens:
            token_index.setdefault(token, set()).add(index)
    for other in competitors:
        tokens = model_tokens(other.get("name", ""))
        if len(tokens) < 4 or not any(any(ch.isdigit() for ch in t) for t in tokens):
            continue
        candidates: set[int] | None = None
        for token in sorted(tokens, key=lambda t: len(token_index.get(t, ()))):
            ids = token_index.get(token) or set()
            candidates = ids if candidates is None else candidates & ids
            if not candidates:
                break
        if not candidates:
            continue
        for index in candidates:
            product, product_words = product_tokens[index]
            if not tokens <= product_words or product["sku"] in linked_skus:
                continue
            converted = other.copy()
            converted["sku"] = product["sku"]
            source_name = other.get("supplier") or "Прайс 482"
            if not source_name.startswith("Прайс 482"):
                source_name = f"Прайс 482 / {source_name}"
            converted["supplier"] = source_name + " (совпадение по модели)"
            key = (converted["sku"], converted["supplier"], converted["price_type"])
            if key in used:
                continue
            used.add(key)
            result.append(converted)
            linked_skus.add(product["sku"])
            matched_name += 1
    return result, matched_sku, matched_name


def parse_products(rows: list[list[str]], mapping: dict) -> tuple[list[dict], list[dict], list[str]]:
    header_row = int(mapping.get("header_row", 1))
    if len(rows) < header_row:
        raise ValueError("В указанной вкладке Google Sheets нет строки заголовков")
    headers = rows[header_row - 1]
    idx = resolve_columns(headers, PRODUCT_FIELDS, mapping.get("columns", {}))
    start_data_row = header_row
    sections_in_sku = False
    if (idx["name"] is None or (idx["retail_price"] is None and idx["wholesale_price"] is None)) and not mapping.get("columns"):
        guess = infer_legacy_columns(rows)
        if guess:
            idx = {key: None for key in PRODUCT_FIELDS}
            idx.update({"sku": guess["sku"], "name": guess["name"], "retail_price": guess["price"]})
            start_data_row = 0

    # Шапка нестандартная (как в реальном прайсе 379) — определяем колонки по данным.
    if idx["name"] is None or (idx["retail_price"] is None and idx["wholesale_price"] is None):
        detected = detect_columns(rows)
        if detected:
            idx = {key: None for key in PRODUCT_FIELDS}
            idx["sku"] = detected["sku"]
            idx["name"] = detected["name"]
            prices = detected["prices"]
            # Одна ценовая колонка = единая цена каталога: показываем её и как
            # розничную, и как оптовую, чтобы не выдумывать второй тариф.
            idx["retail_price"] = prices[0]
            idx["wholesale_price"] = prices[0] if len(prices) == 1 else prices[1]
            start_data_row = 0
            sections_in_sku = True
            log.info("Колонки определены по содержимому: артикул=%s, название=%s, цены=%s",
                     chr(65 + detected["sku"]), chr(65 + detected["name"]),
                     ", ".join(chr(65 + p) for p in prices))

    if idx["name"] is None or (idx["retail_price"] is None and idx["wholesale_price"] is None):
        raise ValueError("Не удалось определить колонки названия и цены. "
                         f"Столбцы Google-таблицы: {headers[:30]!r}. "
                         "Настройте data/column_map.json (header_row, columns).")
    products = []
    other = []
    warnings: list[str] = []
    used: set[str] = set()
    sections = {"category": "", "subcategory": ""}
    if sections_in_sku and rows:
        # Первая строка прайса 379 — заголовок раздела («Код Блоки питания …»),
        # а не товар: в ценовой колонке там текст «Опт цена».
        first_row = rows[start_data_row]
        if not _is_price_cell(cell(first_row, idx["retail_price"])):
            apply_section(cell(first_row, idx["sku"]), sections)
            start_data_row += 1
    for row_no, row in enumerate(rows[start_data_row:], start=start_data_row + 1):
        name = cell(row, idx["name"])
        sku = cell(row, idx["sku"])
        if sections_in_sku:
            if not name:
                apply_section(sku, sections)
                continue
        elif not name:
            continue
        retail = cell(row, idx["retail_price"])
        wholesale = cell(row, idx["wholesale_price"])
        cat = cell(row, idx["category"])
        sub = cell(row, idx["subcategory"])
        if not retail and not wholesale:
            # Строка-разделитель в прайсе может содержать заголовок категории.
            if cat:
                sections["category"] = cat
            if sub:
                sections["subcategory"] = sub
            if name in config.MENU_CATEGORIES:
                sections["category"], sections["subcategory"] = name, ""
            continue
        try:
            r_value = price_number(retail) if retail else None
            w_value = price_number(wholesale) if wholesale else None
        except ValueError as exc:
            if len(warnings) < 15:
                warnings.append(f"строка {row_no}: {exc}")
            continue
        current_category = sections["category"]
        current_subcategory = sections["subcategory"]
        category = cat or current_category or category_guess(name)
        subcategory = sub or (current_subcategory if category == current_category else "") or subcategory_guess(category, name)
        # Для строк без артикула генерируем только ВНУТРЕННИЙ ключ, но не выдаём его за артикул.
        if not sku:
            digest = hashlib.sha256(f"{category}|{subcategory}|{name}|{row_no}".encode()).hexdigest()[:20]
            sku = f"_no_sku_{digest}"
        if sku in used:
            if len(warnings) < 15:
                warnings.append(f"строка {row_no}: артикул {sku} повторяется; строка пропущена")
            continue
        used.add(sku)
        products.append({
            "sku": sku, "name": name, "category": category, "subcategory": subcategory,
            "retail_price": retail, "wholesale_price": wholesale,
            "dns_url": cell(row, idx["dns_url"]), "photo_url": cell(row, idx["photo_url"]),
        })
        compared = cell(row, idx["competitor_price"])
        if compared:
            try:
                price_number(compared)
            except ValueError:
                continue
            if not sku.startswith("_no_sku_"):
                other.append({"sku": sku, "supplier": cell(row, idx["competitor_supplier"]) or "Другой прайс",
                              "price_type": "retail", "price": compared,
                              "url": cell(row, idx["competitor_url"])})
    if not products:
        raise ValueError("В таблице нет ни одной распознанной строки товара. "
                         "Проверьте header_row и columns в data/column_map.json")
    return products, other, warnings


def parse_competitors(rows: list[list[str]], mapping: dict) -> list[dict]:
    """Второй прайс из Google Sheets, включая формат 482 без строки заголовков."""
    header_row = int(mapping.get("other_header_row", mapping.get("header_row", 1)))
    if not rows:
        raise ValueError("Прайс 482 пуст")
    if header_row < 0 or header_row > len(rows):
        raise ValueError("Некорректный other_header_row")
    headers = rows[header_row - 1] if header_row else []
    overrides = mapping.get("other_columns", {})
    idx = resolve_columns(headers, OTHER_FIELDS, overrides)
    start_data_row = header_row
    if (idx["sku"] is None or idx["price"] is None) and not overrides:
        guess = infer_legacy_columns(rows)
        if guess:
            idx = {key: None for key in OTHER_FIELDS}
            idx.update({"sku": guess["sku"], "name": guess["name"], "price": guess["price"]})
            start_data_row = 0
    if idx["sku"] is None or idx["price"] is None:
        raise ValueError("Во втором прайсе не распознаны артикул и цена. "
                         "Укажите other_columns в data/column_map.json, например: "
                         "\"other_header_row\": 0, \"other_columns\": {\"sku\": \"A\", \"name\": \"B\", \"price\": \"C\"}")
    seen = set()
    result = []
    for row in rows[start_data_row:]:
        sku = cell(row, idx["sku"])
        value = cell(row, idx["price"])
        if not sku or not value:
            continue
        try:
            price_number(value)
        except ValueError:
            continue
        price_type = cell(row, idx["price_type"]).casefold()
        price_type = "wholesale" if price_type in ("опт", "оптовая", "wholesale") else "retail"
        supplier = cell(row, idx["supplier"]) or "Прайс 482"
        key = (sku, supplier, price_type)
        if key in seen:
            continue
        seen.add(key)
        result.append({"sku": sku, "name": cell(row, idx["name"]),
                       "supplier": supplier, "price_type": price_type,
                       "price": value, "url": cell(row, idx["url"])})
    if not result:
        raise ValueError("Прайс 482 прочитан, но товары с ценами не распознаны. Проверьте столбцы.")
    return result


def write_csv(path: Path, fields: tuple[str, ...], records: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


class GoogleSheetSync:
    def __init__(self, database: Database):
        self.database = database
        self._lock = RLock()
        self.last_success: str | None = None
        self.last_error: str | None = None
        self.last_columns: list[str] = []

    @staticmethod
    def _fetch_export(sheet_id: str, gid: str) -> list[list[str]]:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
        response = requests.get(url, params={"format": "csv", "gid": gid}, timeout=25,
                                headers={"User-Agent": "ComputerCatalogBot/1.0"})
        response.raise_for_status()
        if "html" in response.headers.get("content-type", "").lower() or response.text.lstrip().startswith("<!DOCTYPE html"):
            raise PermissionError("Google отдал страницу входа вместо CSV. Откройте доступ на чтение "
                                  "по ссылке или настройте GOOGLE_SERVICE_ACCOUNT_FILE")
        if len(response.content) > 18_000_000:
            raise ValueError("Вкладка слишком большая (более 18 МБ)")
        response.encoding = "utf-8-sig"
        return list(csv.reader(io.StringIO(response.text)))

    @staticmethod
    def _fetch_gviz(sheet_id: str, gid: str) -> list[list[str]]:
        """Публичный CSV через Visualization API. Работает там, где /export отдаёт 400."""
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
        response = requests.get(url, params={"tqx": "out:csv", "gid": gid}, timeout=25,
                                headers={"User-Agent": "ComputerCatalogBot/1.0"})
        response.raise_for_status()
        text = response.text
        if not text.strip() or text.lstrip().startswith(("<!DOCTYPE html", "<html")):
            raise PermissionError("Google вернул страницу вместо CSV. Проверьте доступ к таблице.")
        if "google.visualization.Query.setResponse" in text:
            raise ValueError("Вкладка с указанным gid не найдена")
        response.encoding = "utf-8-sig"
        return list(csv.reader(io.StringIO(text)))

    @classmethod
    def _get_rows(cls, sheet_id: str, gid: str) -> list[list[str]]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", sheet_id):
            raise ValueError("Некорректный GOOGLE_SHEET_ID")
        if not re.fullmatch(r"\d+", gid):
            raise ValueError("Некорректный GOOGLE_SHEET_GID")
        credentials_file = config.GOOGLE_SERVICE_ACCOUNT_FILE
        if credentials_file:
            try:
                from google.oauth2 import service_account
                from google.auth.transport.requests import AuthorizedSession
            except ImportError as exc:
                raise RuntimeError("Для приватного Google Sheets установите зависимости requirements.txt") from exc
            creds = service_account.Credentials.from_service_account_file(
                str(credentials_file), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
            )
            with AuthorizedSession(creds) as session:
                base = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
                meta = session.get(base, params={"fields": "sheets(properties(sheetId,title))"}, timeout=20)
                meta.raise_for_status()
                tabs = meta.json().get("sheets", [])
                title = next((s["properties"]["title"] for s in tabs
                              if str(s.get("properties", {}).get("sheetId")) == gid), None)
                if not title:
                    raise ValueError(f"Вкладка с gid={gid} в Google-таблице не найдена")
                safe_title = title.replace("'", "''")
                range_name = quote(f"'{safe_title}'!A:ZZ", safe="")
                response = session.get(f"{base}/values/{range_name}", params={"majorDimension": "ROWS"}, timeout=25)
                response.raise_for_status()
                return [[str(item) for item in row] for row in response.json().get("values", [])]

        # Таблица открыта по ссылке: пробуем /export, затем публичный gviz.
        errors: list[str] = []
        for fetch in (cls._fetch_export, cls._fetch_gviz):
            try:
                rows = fetch(sheet_id, gid)
            except Exception as exc:  # noqa: BLE001 - нужен любой рабочий источник
                errors.append(f"{fetch.__name__}: {exc}")
                continue
            if rows and any(any(cell.strip() for cell in row) for row in rows):
                if len(str(rows)) > 18_000_000:
                    raise ValueError("Вкладка слишком большая (более 18 МБ)")
                return rows
            errors.append(f"{fetch.__name__}: пустой ответ")
        raise RuntimeError("Не удалось получить данные Google Sheets. " + "; ".join(errors))

    def headers(self) -> list[str]:
        rows = self._get_rows(config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_GID)
        mapping = read_mapping(config.COLUMN_MAP_PATH)
        pos = int(mapping.get("header_row", 1)) - 1
        if len(rows) <= pos:
            return []
        return rows[pos]

    def preview_other(self) -> list[list[str]]:
        """Короткий предпросмотр прайса 482 только для администратора."""
        if not config.GOOGLE_OTHER_GID:
            raise ValueError("GOOGLE_OTHER_GID не задан")
        rows = self._get_rows(config.GOOGLE_OTHER_SHEET_ID or config.GOOGLE_SHEET_ID,
                              config.GOOGLE_OTHER_GID)
        return rows[:12]

    def refresh(self) -> tuple[int, int, list[str]]:
        """Обновляет каталог. При ошибке текущий каталог SQLite сохраняется."""
        with self._lock:
            try:
                result = self._refresh()
                self.last_error = None
                return result
            except Exception as exc:
                self.last_error = str(exc)
                raise

    def _refresh(self) -> tuple[int, int, list[str]]:
        mapping = read_mapping(config.COLUMN_MAP_PATH)
        rows = self._get_rows(config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_GID)
        pos = int(mapping.get("header_row", 1)) - 1
        self.last_columns = rows[pos] if len(rows) > pos else []
        products, compared, warnings = parse_products(rows, mapping)
        # Отдельная вкладка другого прайса приоритетнее колонки "цена конкурента".
        if config.GOOGLE_OTHER_GID:
            other_rows = self._get_rows(config.GOOGLE_OTHER_SHEET_ID or config.GOOGLE_SHEET_ID,
                                        config.GOOGLE_OTHER_GID)
            other_items = parse_competitors(other_rows, mapping)
            compared, by_sku, by_name = link_competitors(products, other_items)
            log.info("Прайс 482: %d строк с ценами, совпадений %d по артикулу, %d по названию",
                     len(other_items), by_sku, by_name)
            if not compared:
                warnings.append("Прайс 482 загружен, но совпадений с прайсом 379 не найдено. "
                                "Проверьте, одинаковые ли артикулы или полные названия товаров.")
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sync-", dir=config.DATA_DIR) as work:
            source = Path(work) / "products.csv"
            other_source = Path(work) / "other_prices.csv"
            write_csv(source, PRODUCT_FIELDS[:8], products)
            if config.GOOGLE_OTHER_GID or compared or not config.OTHER_PRICES_PATH.exists():
                write_csv(other_source, OTHER_FIELDS, compared)
            else:
                # Второй прайс также можно вести вручную в data/other_prices.csv.
                other_source = config.OTHER_PRICES_PATH
            product_count, comp_count = self.database.import_prices(source, other_source)
            os.replace(source, config.PRODUCTS_PATH)
            if other_source != config.OTHER_PRICES_PATH:
                os.replace(other_source, config.OTHER_PRICES_PATH)
        self.last_success = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        if warnings:
            log.warning("Ошибки строк Google Sheets (первые 15): %s", "; ".join(warnings))
        return product_count, comp_count, warnings
