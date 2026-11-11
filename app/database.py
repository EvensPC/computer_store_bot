"""SQLite: товары, прайсы, заявки. JSON: постоянный реестр оптовиков."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def price_number(value: str) -> int:
    """Переводит рублёвую цену в целые КОПЕЙКИ без ошибок float.

    Примеры: '12 500 ₽', '12500,50', '12,500.50', '12.500,50'.
    """
    cleaned = str(value).strip().casefold()
    cleaned = re.sub(r"(?i)руб(?:лей|ля|ль|\.)?|₽|р\.", "", cleaned)
    cleaned = cleaned.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in cleaned:
        if cleaned.count(",") == 1 and len(cleaned.split(",")[-1]) <= 2:
            cleaned = cleaned.replace(",", ".")
        elif all(len(part) == 3 for part in cleaned.split(",")[1:]):
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned and len(cleaned.split(".")[-1]) == 3:
        cleaned = cleaned.replace(".", "")
    try:
        amount = Decimal(cleaned)
        copecks = amount * 100
        if (not amount.is_finite() or amount <= 0 or
                copecks != copecks.to_integral_value() or copecks > 10**15):
            raise ValueError("цена должна быть положительным числом не более 2 знаков после запятой")
        return int(copecks)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Некорректная цена: {value!r}") from exc


def csv_rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Не найден прайс: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path.name}: обязательные колонки: {', '.join(sorted(required))}")
        result = []
        for line_no, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"{path.name}, строка {line_no}: лишние разделители")
            if not any((v or "").strip() for v in row.values()):
                continue
            result.append({k: (v or "").strip() for k, v in row.items()})
        return result


class Database:
    def __init__(self, sqlite_path: Path, wholesalers_path: Path):
        self.sqlite_path = Path(sqlite_path)
        self.wholesalers_path = Path(wholesalers_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.wholesalers_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        if not self.wholesalers_path.exists():
            self._write_wholesalers({})
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.sqlite_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL,
                retail_price INTEGER CHECK(retail_price IS NULL OR retail_price > 0),
                wholesale_price INTEGER CHECK(wholesale_price IS NULL OR wholesale_price > 0),
                search_text TEXT NOT NULL,
                dns_url TEXT NOT NULL DEFAULT '',
                photo_url TEXT NOT NULL DEFAULT '',
                CHECK(retail_price IS NOT NULL OR wholesale_price IS NOT NULL)
            );
            CREATE INDEX IF NOT EXISTS idx_products_cat ON products(category,subcategory);
            CREATE TABLE IF NOT EXISTS other_prices (
                sku TEXT NOT NULL,
                supplier TEXT NOT NULL,
                price_type TEXT NOT NULL CHECK(price_type IN ('retail', 'wholesale')),
                price INTEGER NOT NULL CHECK(price > 0),
                url TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (sku, supplier, price_type)
            );
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wholesale_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by INTEGER,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );
            CREATE INDEX IF NOT EXISTS idx_requests_status ON wholesale_requests(status,id DESC);
            """)

    def import_prices(self, products_file: Path, other_prices_file: Path) -> tuple[int, int]:
        """Полная проверка CSV до транзакции. Товары обновляются по SKU без смены ID."""
        required = {"sku", "name", "category", "subcategory", "retail_price", "wholesale_price"}
        source = csv_rows(products_file, required)
        if not source:
            raise ValueError("Основной прайс пуст. Каталог не изменён.")
        products = []
        skus: set[str] = set()
        for i, row in enumerate(source, start=2):
            sku = row["sku"]
            if not sku or not row["name"] or not row["category"] or not row["subcategory"]:
                raise ValueError(f"products.csv, строка {i}: пустой артикул/название/раздел")
            if sku in skus:
                raise ValueError(f"products.csv: повтор артикула {sku}")
            skus.add(sku)
            retail = price_number(row["retail_price"]) if row["retail_price"] else None
            wholesale = price_number(row["wholesale_price"]) if row["wholesale_price"] else None
            if retail is None and wholesale is None:
                raise ValueError(f"products.csv, строка {i}: не указана ни одна цена")
            products.append((sku, row["name"], row["category"], row["subcategory"],
                             retail, wholesale, f"{sku} {row['name']}".casefold(),
                             row.get("dns_url", ""), row.get("photo_url", "")))

        competitors = csv_rows(other_prices_file, {"sku", "supplier", "price", "price_type"})
        others = []
        seen: set[tuple[str, str, str]] = set()
        for i, row in enumerate(competitors, start=2):
            level = row["price_type"].lower()
            if not row["sku"] or not row["supplier"] or level not in ("retail", "wholesale"):
                raise ValueError(f"other_prices.csv, строка {i}: некорректный SKU/поставщик/price_type")
            key = (row["sku"], row["supplier"], level)
            if key in seen:
                raise ValueError(f"other_prices.csv, строка {i}: повтор позиции")
            seen.add(key)
            others.append((*key, price_number(row["price"]), row.get("url", "")))

        with self.connect() as conn:
            for row in products:
                conn.execute("""
                    INSERT INTO products(sku,name,category,subcategory,retail_price,
                                         wholesale_price,search_text,dns_url,photo_url)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(sku) DO UPDATE SET
                      name=excluded.name, category=excluded.category,
                      subcategory=excluded.subcategory, retail_price=excluded.retail_price,
                      wholesale_price=excluded.wholesale_price,search_text=excluded.search_text,
                      dns_url=excluded.dns_url,photo_url=excluded.photo_url
                """, row)
            # Удаляем отсутствующие в новом прайсе товары, но сохраняем ID остальных.
            for old in conn.execute("SELECT sku FROM products"):
                if old["sku"] not in skus:
                    conn.execute("DELETE FROM products WHERE sku=?", (old["sku"],))
            conn.execute("DELETE FROM other_prices")
            conn.executemany("""
                INSERT INTO other_prices(sku,supplier,price_type,price,url) VALUES(?,?,?,?,?)
            """, others)
        return len(products), len(others)

    def db_categories(self) -> list[str]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT category FROM products ORDER BY category")]

    def db_subcategories(self, category: str) -> list[str]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT subcategory FROM products WHERE category=? ORDER BY subcategory", (category,))]

    def count_products(self, category: str, subcategory: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM products WHERE category=?"
        args = [category]
        if subcategory is not None:
            sql += " AND subcategory=?"
            args.append(subcategory)
        with self.connect() as conn:
            return int(conn.execute(sql, args).fetchone()[0])

    def products(self, category: str, subcategory: str, page: int, page_size: int = 8) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT * FROM products WHERE category=? AND subcategory=?
                ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?
            """, (category, subcategory, page_size, max(0, page) * page_size)).fetchall()
            return [dict(r) for r in rows]

    def product(self, product_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            return dict(row) if row else None

    def search(self, text: str, page: int = 0, page_size: int = 8) -> tuple[list[dict], int]:
        escaped = text.casefold().strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM products WHERE search_text LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0]
            rows = conn.execute("""
                SELECT * FROM products WHERE search_text LIKE ? ESCAPE '\\'
                ORDER BY name LIMIT ? OFFSET ?
            """, (pattern, page_size, max(0, page) * page_size)).fetchall()
            return [dict(r) for r in rows], total

    def comparisons(self, sku: str, price_type: str) -> list[dict]:
        """Одна цена 482 является справочной для обоих типов покупателей.

        Это НЕ означает, что цена из 482 — оптовая. Для других поставщиков
        сохраняем обычный фильтр по типу цены.
        """
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("""
                SELECT * FROM other_prices
                WHERE sku=? AND (price_type=? OR (price_type='retail' AND supplier LIKE 'Прайс 482%'))
                ORDER BY price ASC, supplier ASC
            """, (sku, price_type))]

    def save_user(self, telegram_id: int, full_name: str, username: str = ""):
        now = utc_now()
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO users(telegram_id,full_name,username,first_seen,last_seen)
                VALUES(?,?,?,?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name,
                    username=excluded.username,last_seen=excluded.last_seen
            """, (telegram_id, full_name, username, now, now))

    def user(self, telegram_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            return dict(row) if row else None

    def _read_wholesalers(self) -> dict[str, dict]:
        with self.wholesalers_path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            raise ValueError("wholesalers.json должен содержать JSON-объект")
        return obj

    def _write_wholesalers(self, data: dict[str, dict]):
        # Атомарная замена файла, чтобы не оставить его частично записанным.
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.wholesalers_path.parent,
                prefix=".wholesalers-", suffix=".tmp", delete=False
            ) as f:
                tmp_path = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.wholesalers_path)
            try:
                self.wholesalers_path.chmod(0o600)
            except OSError:
                pass
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def export_wholesalers_csv(self, dest: Path) -> None:
        """Экспорт отдельного реестра в UTF-8 CSV для Excel."""
        fields = ["telegram_id", "full_name", "username", "approved_at", "approved_by"]
        with self._lock:
            rows = self.wholesalers()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                             dir=dest.parent, prefix=".export-",
                                             suffix=".csv", delete=False) as f:
                tmp = Path(f.name)
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            try:
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)

    def is_wholesaler(self, telegram_id: int) -> bool:
        with self._lock:
            return str(telegram_id) in self._read_wholesalers()

    def wholesalers(self) -> list[dict]:
        with self._lock:
            return sorted(self._read_wholesalers().values(),
                          key=lambda item: item.get("approved_at", ""), reverse=True)

    def latest_request(self, telegram_id: int) -> dict | None:
        with self.connect() as conn:
            r = conn.execute("""
                SELECT * FROM wholesale_requests WHERE telegram_id=? ORDER BY id DESC LIMIT 1
            """, (telegram_id,)).fetchone()
            return dict(r) if r else None

    def request_wholesale(self, telegram_id: int) -> tuple[dict, bool]:
        """(заявка, is_new). Если pending уже есть, повторную не создаём."""
        with self.connect() as conn:
            existing = conn.execute("""
                SELECT * FROM wholesale_requests WHERE telegram_id=? AND status='pending'
                ORDER BY id DESC LIMIT 1
            """, (telegram_id,)).fetchone()
            if existing:
                return dict(existing), False
            cursor = conn.execute("""
                INSERT INTO wholesale_requests(telegram_id,status,created_at)
                VALUES(?,'pending',?)
            """, (telegram_id, utc_now()))
            row = conn.execute("SELECT * FROM wholesale_requests WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row), True

    def request_by_id(self, request_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT r.*, u.full_name, u.username FROM wholesale_requests r
                LEFT JOIN users u ON u.telegram_id=r.telegram_id WHERE r.id=?
            """, (request_id,)).fetchone()
            return dict(row) if row else None

    def requests(self, page: int = 0, pending_only: bool = True, page_size: int = 5) -> tuple[list[dict], int]:
        where = "WHERE r.status='pending'" if pending_only else ""
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM wholesale_requests r {where}").fetchone()[0]
            rows = conn.execute(f"""
                SELECT r.*, u.full_name, u.username FROM wholesale_requests r
                LEFT JOIN users u ON r.telegram_id=u.telegram_id
                {where} ORDER BY r.id DESC LIMIT ? OFFSET ?
            """, (page_size, max(0, page) * page_size)).fetchall()
            return [dict(r) for r in rows], total

    def review_request(self, request_id: int, approve: bool, admin_id: int) -> dict | None:
        with self._lock:
            with self.connect() as conn:
                row = conn.execute("""
                    SELECT r.*, u.full_name, u.username FROM wholesale_requests r
                    LEFT JOIN users u ON r.telegram_id=u.telegram_id WHERE r.id=?
                """, (request_id,)).fetchone()
                if not row or row["status"] != "pending":
                    return None
                if approve:
                    users = self._read_wholesalers()
                    users[str(row["telegram_id"])] = {
                        "telegram_id": row["telegram_id"],
                        "full_name": row["full_name"] or "",
                        "username": row["username"] or "",
                        "approved_at": utc_now(),
                        "approved_by": admin_id,
                    }
                    self._write_wholesalers(users)
                conn.execute("""
                    UPDATE wholesale_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?
                """, ("approved" if approve else "rejected", utc_now(), admin_id, request_id))
                return dict(row)

    def remove_wholesaler(self, telegram_id: int) -> bool:
        with self._lock:
            users = self._read_wholesalers()
            if users.pop(str(telegram_id), None) is None:
                return False
            self._write_wholesalers(users)
            return True
