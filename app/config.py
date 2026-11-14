"""Настройки, ID Google Sheets и пути к постоянным данным."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS: set[int] = {
    int(item.strip()) for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.sqlite3"
WHOLESALERS_PATH = DATA_DIR / "wholesalers.json"
WHOLESALERS_CSV_PATH = DATA_DIR / "wholesalers.csv"
PRODUCTS_PATH = DATA_DIR / "products.csv"
OTHER_PRICES_PATH = DATA_DIR / "other_prices.csv"
COLUMN_MAP_PATH = DATA_DIR / "column_map.json"

# Точная ссылка, указанная владельцем прайса.
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1YloYvrGUqWY45khtZUVKbfmAupR1PotJhXUTTTP--JA").strip()
GOOGLE_SHEET_GID = os.getenv("GOOGLE_SHEET_GID", "298640172").strip()
GOOGLE_OTHER_SHEET_ID = (os.getenv("GOOGLE_OTHER_SHEET_ID", "").strip() or "1aI1SuXZ2PfiKL4d4neY0xYRuWSHybS0PD1z3rFOnnM8")
GOOGLE_OTHER_GID = (os.getenv("GOOGLE_OTHER_GID", "").strip() or "0")
service_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = Path(service_file).expanduser().resolve() if service_file else None
SYNC_INTERVAL_MINUTES = max(1, int(os.getenv("SYNC_INTERVAL_MINUTES", "15")))

# --- Режим работы: long polling (VPS, локально) или webhook (Render, PaaS) ---
# Render сам подставляет RENDER_EXTERNAL_URL, поэтому на нём webhook включается автоматически.
WEBHOOK_URL = (os.getenv("WEBHOOK_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip() or "/telegram/webhook"
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH
_webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
if not _webhook_secret and BOT_TOKEN:
    # Секрет нужен для проверки, что запрос пришёл именно от Telegram.
    import hashlib

    _webhook_secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:40]
WEBHOOK_SECRET = _webhook_secret
PORT = int(os.getenv("PORT", "8080"))
USE_WEBHOOK = bool(WEBHOOK_URL)

# --- Оптовики и заявки в Google-таблице (резервное хранилище + ручное редактирование) ---
# Если WHOLESALE_SHEET_ID пуст, всё работает как раньше: только SQLite и wholesalers.json.
WHOLESALE_SHEET_ID = os.getenv("WHOLESALE_SHEET_ID", "").strip()
WHOLESALE_TAB_WHOLESALERS = os.getenv("WHOLESALE_TAB_WHOLESALERS", "Оптовики").strip() or "Оптовики"
WHOLESALE_TAB_REQUESTS = os.getenv("WHOLESALE_TAB_REQUESTS", "Заявки").strip() or "Заявки"

# Категории прайса 379 в том порядке, в котором они идут в таблице.
# Любой раздел таблицы, которого здесь нет, считается подкатегорией предыдущей категории.
MENU_CATEGORIES = (
    "Блоки питания",
    "Видеокарты",
    "Жесткие диски и SSD",
    "Материнские платы",
    "Оперативная память",
    "Процессоры",
    "Системы охлаждения",
    "Корпуса",
    "Оптические приводы",
    "Звуковые карты",
    "Контроллеры, конверторы, планки портов",
    "Комплектующие Б/у",
    "Мониторы",
    "Периферия",
    "Накопители данных",
    "Компьютеры и ноутбуки",
    "Сетевое оборудование",
    "Кабели и переходники",
    "Программное обеспечение",
    "Разное",
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
