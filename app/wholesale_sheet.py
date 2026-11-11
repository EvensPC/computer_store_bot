"""Двусторонняя синхронизация оптовиков и истории заявок с Google-таблицей.

Зачем: на бесплатном плане Render папка `data/` стирается при каждом передеплое,
поэтому SQLite и wholesalers.json не могут быть единственным хранилищем.
Google-таблица выступает резервным хранилищем и позволяет править статусы вручную.

Правила:
* локальные SQLite/JSON остаются рабочей копией — бот никогда не ждёт Google;
* таблица догоняет локальное состояние при каждом изменении и раз в N минут;
* при старте данные из таблицы вливаются в локальные (это и есть восстановление);
* отзыв доступа хранится отдельно (`wholesaler_revocations`), чтобы неудачная
  запись в таблицу не «воскресила» пользователя после перезапуска.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import RLock
from urllib.parse import quote

from . import config
from .database import Database, utc_now

log = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

WHOLESALER_FIELDS = ("telegram_id", "full_name", "username", "approved_at",
                     "approved_by", "status", "updated_at")
REQUEST_FIELDS = ("id", "telegram_id", "full_name", "username", "status",
                  "created_at", "reviewed_at", "reviewed_by")

ACTIVE_STATUS = "active"
REVOKED_STATUS = "revoked"
REVOKED_WORDS = {
    "revoked", "отозван", "отозвано", "отозван доступ", "удалён", "удален",
    "заблокирован", "нет", "no", "false",
}
REQUEST_STATUSES = {"pending", "approved", "rejected"}
STATUS_ALIASES = {
    "ожидает": "pending", "на рассмотрении": "pending", "pending": "pending",
    "одобрена": "approved", "одобрен": "approved", "approved": "approved",
    "отклонена": "rejected", "отклонён": "rejected", "rejected": "rejected",
}


def _digits(value: object) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def _int_or_none(value: object) -> int | None:
    text = _digits(value)
    return int(text) if text else None


def _int_or_text(value: object) -> int | str:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text


def normalize_wholesaler_status(value: object) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ACTIVE_STATUS
    return REVOKED_STATUS if text in REVOKED_WORDS else ACTIVE_STATUS


def normalize_request_status(value: object) -> str:
    text = str(value or "").strip().casefold()
    return STATUS_ALIASES.get(text, text)


def _stamp(row: dict) -> str:
    """Строка-время для сравнения «кто новее». Формат UTC сортируется лексикографически."""
    return str(row.get("reviewed_at") or row.get("created_at") or row.get("updated_at") or "")


def merge_wholesalers(local_active: dict[str, dict], local_revoked: dict[str, str],
                      sheet_rows: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """Сливает реестр из таблицы с локальным, не теряя отзывы доступа.

    Возвращает (активные, отозванные). Отозванные нужны, чтобы отзыв,
    сделанный в таблице вручную, тоже попал в локальное хранилище.
    """
    active = {str(key): dict(value) for key, value in local_active.items()}
    revoked = {str(key): str(value) for key, value in local_revoked.items()}

    for row in sheet_rows:
        telegram_id = _digits(row.get("telegram_id"))
        if not telegram_id:
            continue
        status = normalize_wholesaler_status(row.get("status"))
        stamp = str(row.get("updated_at") or row.get("approved_at") or "").strip()
        if status == REVOKED_STATUS:
            active.pop(telegram_id, None)
            revoked[telegram_id] = stamp or utc_now()
            continue
        local_revoked_at = revoked.get(telegram_id)
        if local_revoked_at and local_revoked_at >= stamp:
            # Локальный отзыв новее строки таблицы — оставляем доступ отозванным.
            continue
        revoked.pop(telegram_id, None)
        active[telegram_id] = {
            "telegram_id": int(telegram_id),
            "full_name": str(row.get("full_name") or "").strip(),
            "username": str(row.get("username") or "").strip().lstrip("@"),
            "approved_at": stamp or utc_now(),
            "approved_by": _int_or_text(row.get("approved_by")),
        }
    return active, revoked


def merge_requests(local_rows: list[dict], sheet_rows: list[dict]) -> list[dict]:
    """Заявки сливаются по ID. Побеждает более «поздняя» версия строки."""
    merged: dict[int, dict] = {}
    for row in local_rows:
        raw_id = _digits(row.get("id"))
        if raw_id:
            merged[int(raw_id)] = dict(row)

    for row in sheet_rows:
        raw_id = _digits(row.get("id"))
        if not raw_id:
            continue
        status = normalize_request_status(row.get("status"))
        if status not in REQUEST_STATUSES:
            continue
        telegram_id = _digits(row.get("telegram_id"))
        if not telegram_id:
            continue
        candidate = {
            "id": int(raw_id),
            "telegram_id": int(telegram_id),
            "status": status,
            "created_at": str(row.get("created_at") or "").strip(),
            "reviewed_at": str(row.get("reviewed_at") or "").strip() or None,
            "reviewed_by": _int_or_none(row.get("reviewed_by")),
            "full_name": str(row.get("full_name") or "").strip(),
            "username": str(row.get("username") or "").strip().lstrip("@"),
        }
        current = merged.get(int(raw_id))
        if current is None:
            merged[int(raw_id)] = candidate
            continue
        current_rank = 1 if current.get("status") in ("approved", "rejected") else 0
        candidate_rank = 1 if candidate["status"] in ("approved", "rejected") else 0
        if candidate_rank > current_rank:
            merged[int(raw_id)] = candidate
        elif candidate_rank == current_rank and _stamp(candidate) > _stamp(current):
            merged[int(raw_id)] = candidate
        # Иначе оставляем локальную версию: она свежее.
    return [merged[key] for key in sorted(merged)]


class WholesaleSheet:
    """Синхронизация реестра оптовиков и заявок с вкладками Google-таблицы."""

    def __init__(self, database: Database):
        self.database = database
        self._lock = RLock()
        self.last_success: str | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(config.WHOLESALE_SHEET_ID)

    # ---------- низкоуровневый доступ к Google Sheets ----------

    @classmethod
    def _api(cls, suffix: str = "") -> str:
        return f"{SHEETS_API}/{config.WHOLESALE_SHEET_ID}{suffix}"

    @staticmethod
    def _session():
        credentials_file = config.GOOGLE_SERVICE_ACCOUNT_FILE
        if not credentials_file:
            raise RuntimeError(
                "Для записи оптовиков в Google-таблицу нужен сервисный аккаунт: "
                "укажите GOOGLE_SERVICE_ACCOUNT_FILE в .env и дайте его email права "
                "редактора на таблицу."
            )
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise RuntimeError("Установите зависимости из requirements.txt") from exc
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_file), scopes=[SHEETS_SCOPE]
        )
        return AuthorizedSession(credentials)

    def _ensure_tabs(self, session) -> None:
        response = session.get(self._api(), params={"fields": "sheets(properties(title))"}, timeout=20)
        response.raise_for_status()
        existing = {item["properties"]["title"] for item in response.json().get("sheets", [])}
        missing = [name for name in (config.WHOLESALE_TAB_WHOLESALERS, config.WHOLESALE_TAB_REQUESTS)
                   if name not in existing]
        if not missing:
            return
        body = {"requests": [{"addSheet": {"properties": {"title": name}}} for name in missing]}
        created = session.post(self._api(":batchUpdate"), json=body, timeout=20)
        created.raise_for_status()
        log.info("В Google-таблице созданы вкладки: %s", ", ".join(missing))

    def _read_tab(self, session, title: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
        range_name = quote(f"'{title}'!A1:ZZ20000", safe="")
        response = session.get(self._api(f"/values/{range_name}"),
                               params={"majorDimension": "ROWS"}, timeout=30)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        rows = response.json().get("values", [])
        if not rows:
            return []
        header = [str(value).strip().casefold() for value in rows[0]]
        result: list[dict[str, str]] = []
        for row in rows[1:]:
            if not any(str(value).strip() for value in row):
                continue
            item = {}
            for name in fields:
                index = header.index(name.casefold()) if name.casefold() in header else -1
                item[name] = str(row[index]).strip() if 0 <= index < len(row) else ""
            result.append(item)
        return result

    def _write_tab(self, session, title: str, fields: tuple[str, ...], rows: list[dict]) -> None:
        previous = len(self._read_tab(session, title, fields))
        values = [list(fields)]
        for row in rows:
            values.append([str(row.get(name, "")) for name in fields])
        range_name = quote(f"'{title}'!A1", safe="")
        response = session.put(
            self._api(f"/values/{range_name}"),
            params={"valueInputOption": "RAW", "majorDimension": "ROWS"},
            json={"range": f"'{title}'!A1", "majorDimension": "ROWS", "values": values},
            timeout=40,
        )
        response.raise_for_status()
        if previous > len(rows):
            # Стираем «хвост» от прошлой, более длинной версии таблицы.
            stale = quote(f"'{title}'!A{len(rows) + 2}:ZZ{previous + 1}", safe="")
            session.post(self._api(f"/values/{stale}:clear"), timeout=30)

    # ---------- построение строк для записи ----------

    def _wholesaler_rows(self) -> list[dict]:
        rows = []
        for item in self.database.wholesalers():
            rows.append({
                "telegram_id": item.get("telegram_id", ""),
                "full_name": item.get("full_name", ""),
                "username": item.get("username", ""),
                "approved_at": item.get("approved_at", ""),
                "approved_by": item.get("approved_by", ""),
                "status": ACTIVE_STATUS,
                "updated_at": item.get("approved_at", ""),
            })
        for item in self.database.revoked_wholesalers():
            rows.append({
                "telegram_id": item.get("telegram_id", ""),
                "full_name": item.get("full_name", ""),
                "username": item.get("username", ""),
                "approved_at": "",
                "approved_by": item.get("revoked_by", ""),
                "status": REVOKED_STATUS,
                "updated_at": item.get("revoked_at", ""),
            })
        return rows

    def _request_rows(self) -> list[dict]:
        rows = []
        for item in self.database.all_requests():
            rows.append({
                "id": item.get("id", ""),
                "telegram_id": item.get("telegram_id", ""),
                "full_name": item.get("full_name", "") or "",
                "username": item.get("username", "") or "",
                "status": item.get("status", ""),
                "created_at": item.get("created_at", "") or "",
                "reviewed_at": item.get("reviewed_at", "") or "",
                "reviewed_by": item.get("reviewed_by", "") or "",
            })
        return rows

    # ---------- публичные операции ----------

    def push(self) -> tuple[int, int]:
        """Пишет текущее локальное состояние в таблицу."""
        if not self.enabled:
            return 0, 0
        with self._lock:
            try:
                session = self._session()
                self._ensure_tabs(session)
                wholesalers = self._wholesaler_rows()
                requests = self._request_rows()
                self._write_tab(session, config.WHOLESALE_TAB_WHOLESALERS, WHOLESALER_FIELDS, wholesalers)
                self._write_tab(session, config.WHOLESALE_TAB_REQUESTS, REQUEST_FIELDS, requests)
            except Exception as exc:
                self.last_error = str(exc)
                raise
            self.last_error = None
            self.last_success = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
            log.info("Оптовики и заявки записаны в Google-таблицу: %d / %d",
                     len(wholesalers), len(requests))
            return len(wholesalers), len(requests)

    def restore(self) -> tuple[int, int]:
        """Вливает данные из таблицы в локальное хранилище."""
        if not self.enabled:
            return 0, 0
        with self._lock:
            try:
                session = self._session()
                self._ensure_tabs(session)
                sheet_wholesalers = self._read_tab(
                    session, config.WHOLESALE_TAB_WHOLESALERS, WHOLESALER_FIELDS)
                sheet_requests = self._read_tab(
                    session, config.WHOLESALE_TAB_REQUESTS, REQUEST_FIELDS)
            except Exception as exc:
                self.last_error = str(exc)
                raise
            self.last_error = None

        local_active = {str(item["telegram_id"]): item for item in self.database.wholesalers()}
        local_revoked = self.database.revocation_dates()
        active, revoked = merge_wholesalers(local_active, local_revoked, sheet_wholesalers)
        self.database.replace_wholesalers(active)
        for telegram_id, revoked_at in revoked.items():
            self.database.record_revocation(int(telegram_id), revoked_at=revoked_at)
        merged_requests = merge_requests(self.database.all_requests(), sheet_requests)
        for row in merged_requests:
            self.database.upsert_request(row)
        log.info("Из Google-таблицы восстановлено: активных оптовиков %d, отозванных %d, заявок %d",
                 len(active), len(revoked), len(merged_requests))
        return len(active), len(merged_requests)

    def restore_and_push(self) -> tuple[int, int]:
        self.restore()
        return self.push()
