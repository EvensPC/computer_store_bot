"""Проверка синхронизации оптовиков и заявок с Google-таблицей без сети.

Google Sheets API подменяется простым хранилищем в памяти, поэтому тест
проверяет реальную логику записи, восстановления и слияния статусов.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from app import config
from app.database import Database
from app.wholesale_sheet import (ACTIVE_STATUS, REVOKED_STATUS, WholesaleSheet,
                                 merge_requests, merge_wholesalers)

SHEET_ID = "test_sheet_id"


class FakeResponse:
    def __init__(self, payload: dict | None = None, status: int = 200):
        self._payload = payload or {}
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSheets:
    """Минимальная имитация Sheets API: вкладки и значения в памяти."""

    def __init__(self):
        self.tabs: dict[str, list[list[str]]] = {}

    @staticmethod
    def _title(url: str) -> str:
        raw = unquote(url.split("/values/", 1)[1])
        return raw.split("!", 1)[0].strip("'")

    def get(self, url, params=None, timeout=None):
        if "/values/" in url:
            return FakeResponse({"values": self.tabs.get(self._title(url), [])})
        return FakeResponse({"sheets": [{"properties": {"title": t}} for t in self.tabs]})

    def post(self, url, json=None, timeout=None):
        if ":batchUpdate" in url:
            for request in (json or {}).get("requests", []):
                self.tabs.setdefault(request["addSheet"]["properties"]["title"], [])
            return FakeResponse({"replies": []})
        if url.endswith(":clear"):
            title = self._title(url)
            cells = unquote(url.split("/values/", 1)[1]).split(":", 1)[1]
            start_row = int(re.match(r"[A-Z]+(\d+)", cells).group(1))
            self.tabs[title] = self.tabs[title][: start_row - 1]
            return FakeResponse({})
        return FakeResponse({})

    def put(self, url, params=None, json=None, timeout=None):
        title = json["range"].split("!", 1)[0].strip("'")
        self.tabs[title] = [list(row) for row in json["values"]]
        return FakeResponse({})


class WholesaleSheetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.old_sheet_id = config.WHOLESALE_SHEET_ID
        config.WHOLESALE_SHEET_ID = SHEET_ID
        config.WHOLESALE_TAB_WHOLESALERS = "Оптовики"
        config.WHOLESALE_TAB_REQUESTS = "Заявки"
        self.fake = FakeSheets()
        self.db = Database(self.dir / "bot.sqlite3", self.dir / "wholesalers.json")
        self.sheet = self._sheet(self.db, self.fake)

    def tearDown(self):
        config.WHOLESALE_SHEET_ID = self.old_sheet_id

    @staticmethod
    def _sheet(db: Database, fake: FakeSheets) -> WholesaleSheet:
        sheet = WholesaleSheet(db)
        sheet._session = lambda: fake
        return sheet

    def _approve(self, telegram_id: int, name: str = "Тест"):
        self.db.save_user(telegram_id, name, f"user{telegram_id}")
        request, _ = self.db.request_wholesale(telegram_id)
        self.db.review_request(request["id"], True, admin_id=111)

    def test_push_creates_tabs_with_headers(self):
        self._approve(555)
        wholesalers, requests = self.sheet.push()
        self.assertEqual((wholesalers, requests), (1, 1))
        heads = self.fake.tabs["Оптовики"][0]
        self.assertEqual(heads[0], "telegram_id")
        self.assertIn("status", heads)
        self.assertEqual(self.fake.tabs["Оптовики"][1][0], "555")
        self.assertEqual(self.fake.tabs["Оптовики"][1][heads.index("status")], ACTIVE_STATUS)
        self.assertEqual(self.fake.tabs["Заявки"][1][0], "1")

    def test_fresh_environment_restores_from_sheet(self):
        self._approve(555, "Иван")
        self.sheet.push()

        # Имитация передеплоя на Render: локальные данные полностью потеряны.
        fresh_db = Database(self.dir / "fresh.sqlite3", self.dir / "fresh.json")
        fresh_sheet = self._sheet(fresh_db, self.fake)
        self.assertFalse(fresh_db.is_wholesaler(555))

        fresh_sheet.restore()

        self.assertTrue(fresh_db.is_wholesaler(555))
        item = fresh_db.wholesalers()[0]
        self.assertEqual(item["telegram_id"], 555)
        self.assertEqual(item["full_name"], "Иван")
        history = fresh_db.all_requests()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "approved")

    def test_revoked_row_in_sheet_is_not_restored(self):
        self._approve(555)
        self.sheet.push()
        # Администратор вручную ставит статус revoked прямо в таблице.
        rows = self.fake.tabs["Оптовики"]
        rows[1][rows[0].index("status")] = REVOKED_STATUS

        fresh_db = Database(self.dir / "fresh.sqlite3", self.dir / "fresh.json")
        self._sheet(fresh_db, self.fake).restore()

        self.assertFalse(fresh_db.is_wholesaler(555))
        self.assertIn("555", fresh_db.revocation_dates())

    def test_local_revocation_survives_stale_sheet_row(self):
        self._approve(555)
        self.db.remove_wholesaler(555, admin_id=111)
        self.assertTrue(self.db.wholesalers() == [])
        # Таблица всё ещё содержит активную строку (push не успел пройти).
        stale = [{
            "telegram_id": "555", "full_name": "Иван", "username": "",
            "approved_at": "2026-01-01 00:00 UTC", "approved_by": "111",
            "status": ACTIVE_STATUS, "updated_at": "2026-01-01 00:00 UTC",
        }]
        active, revoked = merge_wholesalers({}, self.db.revocation_dates(), stale)
        self.assertNotIn("555", active)
        self.assertIn("555", revoked)

    def test_newer_sheet_revocation_wins_over_local_active(self):
        local = {"555": {"telegram_id": 555, "full_name": "Иван", "username": "",
                         "approved_at": "2026-01-01 00:00 UTC", "approved_by": 111}}
        active, revoked = merge_wholesalers(local, {}, [{
            "telegram_id": "555", "full_name": "Иван", "username": "",
            "approved_at": "2026-01-01 00:00 UTC", "approved_by": "111",
            "status": REVOKED_STATUS, "updated_at": "2026-05-05 10:00 UTC",
        }])
        self.assertNotIn("555", active)
        self.assertEqual(revoked["555"], "2026-05-05 10:00 UTC")

    def test_request_merge_keeps_newer_review_result(self):
        local = [{"id": 7, "telegram_id": 555, "status": "pending",
                  "created_at": "2026-01-01 00:00 UTC", "reviewed_at": None}]
        sheet = [{"id": "7", "telegram_id": "555", "full_name": "Иван", "username": "",
                  "status": "approved", "created_at": "2026-01-01 00:00 UTC",
                  "reviewed_at": "2026-01-02 00:00 UTC", "reviewed_by": "111"}]
        merged = merge_requests(local, sheet)
        self.assertEqual(merged[0]["status"], "approved")
        self.assertEqual(merged[0]["reviewed_by"], 111)

    def test_request_merge_ignores_older_sheet_row(self):
        local = [{"id": 7, "telegram_id": 555, "status": "approved",
                  "created_at": "2026-01-01 00:00 UTC",
                  "reviewed_at": "2026-02-02 00:00 UTC", "reviewed_by": 111}]
        sheet = [{"id": "7", "telegram_id": "555", "full_name": "Иван", "username": "",
                  "status": "pending", "created_at": "2026-01-01 00:00 UTC",
                  "reviewed_at": "", "reviewed_by": ""}]
        merged = merge_requests(local, sheet)
        self.assertEqual(merged[0]["status"], "approved")

    def test_stale_tail_rows_are_cleared(self):
        self._approve(555)
        self._approve(556)
        self.sheet.push()
        self.assertEqual(len(self.fake.tabs["Оптовики"]), 3)

        self.db.remove_wholesaler(555, admin_id=111)
        self.db.remove_wholesaler(556, admin_id=111)
        self.sheet.push()

        # Остались только отозванные строки, «хвост» прошлой версии стёрт.
        statuses = [row[6] for row in self.fake.tabs["Оптовики"][1:]]
        self.assertNotIn(ACTIVE_STATUS, statuses)


if __name__ == "__main__":
    unittest.main()
