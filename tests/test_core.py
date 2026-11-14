import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.database import Database, price_number
from app.sheet_sync import GoogleSheetSync, parse_products, parse_competitors, link_competitors


class PricesTest(unittest.TestCase):
    def test_ruble_prices_to_kopecks(self):
        cases = {
            "12 500 ₽": 1_250_000,
            "12500,50": 1_250_050,
            "12,500.50": 1_250_050,
            "12.500,50": 1_250_050,
            "100": 10_000,
        }
        for source, value in cases.items():
            with self.subTest(source=source):
                self.assertEqual(price_number(source), value)
        for bad in ["-30", "0", "12,1234", "не цена"]:
            with self.assertRaises(ValueError):
                price_number(bad)

    def test_sheet_parsing_and_no_fake_prices(self):
        data = [
            ["Артикул", "Наименование", "Группа", "Подгруппа", "Розничная цена", "Оптовая цена", "Цена конкурента"],
            ["RTX-101", "GeForce RTX 4060", "Видеокарты", "NVIDIA", "34 500", "33 000", "35 300"],
            ["S-2", "SSD NVMe 1 TB", "", "", "6 490", "", ""],
            ["", "Товар без артикула", "", "", "350", "", ""],
        ]
        products, other, warnings = parse_products(data, {"header_row": 1, "columns": {}})
        self.assertEqual(len(products), 3)
        self.assertEqual(len(other), 1)
        self.assertEqual(products[0]["category"], "Видеокарты")
        self.assertEqual(products[1]["category"], "Жесткие диски и SSD")
        self.assertEqual(products[1]["wholesale_price"], "")
        self.assertTrue(products[2]["sku"].startswith("_no_sku_"))
        self.assertEqual(warnings, [])

    def test_headerless_482_price_and_safe_matching(self):
        # В прайсе 482 сверху текст, далее разделы, затем A=артикул,
        # B=название, C=цена, D=остаток. Не перепутать цену и остаток.
        other_rows = [
            ["", "с 10-00 до 19-00 ЦЕНЫ И НАЛИЧИЕ АКТУАЛЬНЫ!!!", "", ""],
            ["", "Комплектующие", "", ""],
            ["", "Процессоры", "", ""],
            ["5779", "Процессор Intel Pentium G3260 OEM", "1350", "4"],
            ["5969", "Процессор Intel Celeron G3900 OEM", "800", "20"],
            ["7893", "Процессор Intel Celeron G3930 BOX", "950", "5"],
            ["5838", "Процессор Intel Pentium G4400 OEM", "1190", "6"],
        ]
        others = parse_competitors(other_rows, {"header_row": 1})
        self.assertEqual(len(others), 4)
        self.assertEqual(others[0]["price"], "1350")
        self.assertEqual(others[0]["sku"], "5779")
        products = [
            {"sku": "5779", "name": "Процессор Intel Pentium G3260 OEM"},
            {"sku": "379-200", "name": "Процессор Intel Celeron G3900 OEM"},
            {"sku": "7893", "name": "ДРУГАЯ МОДЕЛЬ"},
        ]
        linked, by_sku, by_name = link_competitors(products, others)
        self.assertEqual((by_sku, by_name), (2, 1))
        self.assertEqual(len(linked), 3)
        self.assertEqual(linked[1]["sku"], "379-200")
        self.assertIn("по названию", linked[1]["supplier"])
        self.assertIn("проверьте название", linked[2]["supplier"])

    def test_headerless_main_379_with_one_price_is_not_fake_wholesale(self):
        rows = [
            ["", "Комплектующие", ""],
            ["", "Процессоры", ""],
            ["A100", "Процессор Ryzen 5 5600 OEM", "8900"],
            ["A101", "Процессор Ryzen 7 5700 OEM", "11200"],
        ]
        products, others, warnings = parse_products(rows, {"header_row": 1, "columns": {}})
        self.assertEqual(len(products), 2)
        self.assertEqual(products[0]["retail_price"], "8900")
        self.assertEqual(products[0]["wholesale_price"], "")
        self.assertEqual(products[0]["category"], "Процессоры")

    def test_competitor_tab(self):
        data = [["Артикул", "Поставщик", "Цена", "Тип цены"], ["X2", "Компания", "4 000", "опт"]]
        rows = parse_competitors(data, {"header_row": 1})
        self.assertEqual(rows[0]["price_type"], "wholesale")

    def test_real_379_layout_with_sections_in_sku_column(self):
        # Реальная структура прайса 379: артикул в B, название в C,
        # единственная цена в D, а разделы идут в колонке B без названия.
        rows = [
            ["", "Код Блоки питания 1000-2200W", "Обновлен 11.11.26 EvensPC", "Опт цена ", ""],
            ["", "00-00007577", "Блок питания 1000W 1stPlayer NGDP", "10500", ""],
            ["", "00-00007852", "Блок питания 1000W ASUS TUF", "17500", ""],
            ["", "800-850W", "", "", ""],
            ["", "00-00009000", "Блок питания 850W Chieftec", "7000", ""],
            ["", "Видеокарты", "", "", ""],
            ["", "Видеокарты NVIDIA", "", "", ""],
            ["", "00-00001234", "Видеокарта RTX 4060", "34500", ""],
            ["", "00-00001235", "Видеокарта RTX 4070", "54500", ""],
        ]
        products, other, warnings = parse_products(rows, {"header_row": 1, "columns": {}})
        self.assertEqual(len(products), 5)
        self.assertEqual(other, [])
        self.assertEqual(products[0]["sku"], "00-00007577")
        self.assertEqual(products[0]["category"], "Блоки питания")
        self.assertEqual(products[0]["subcategory"], "1000-2200W")
        self.assertEqual(products[0]["retail_price"], "10500")
        self.assertEqual(products[0]["wholesale_price"], "10500")
        self.assertEqual(products[2]["subcategory"], "800-850W")
        self.assertEqual(products[3]["category"], "Видеокарты")
        self.assertEqual(products[3]["subcategory"], "Видеокарты NVIDIA")
        self.assertTrue(all("Опт цена" not in (p["name"] or "") for p in products))
        self.assertEqual(warnings, [])


    def test_model_matching_rejects_different_versions(self):
        products = [
            {"sku": "A1", "name": "Видеокарта GeForce RTX 3050 6GB MSI VENTUS 2X E OC"},
            {"sku": "A2", "name": "Материнская плата LGA 1700 ASUS PRIME B760M-A WIFI D4 (ATX, 4xDDR4)"},
            {"sku": "A3", "name": "Блок питания 850W 1stPlayer NGDP Gold (HA-850BA4-WH)"},
        ]
        competitors = [
            {"sku": "9001", "name": "Видеокарта GeForce RTX 5060Ti 16Gb MSI Ventus 2X OC Plus",
             "supplier": "Прайс 482", "price_type": "retail", "price": "84000", "url": ""},
            {"sku": "9002", "name": "Материнская плата 1700 Asus B760M-F Prime",
             "supplier": "Прайс 482", "price_type": "retail", "price": "9500", "url": ""},
            {"sku": "9003", "name": "Блок питания 850W 1stPlayer NGDP HA-850BA4-BK",
             "supplier": "Прайс 482", "price_type": "retail", "price": "7250", "url": ""},
        ]
        linked, by_sku, by_name = link_competitors(products, competitors)
        self.assertEqual(linked, [])
        self.assertEqual((by_sku, by_name), (0, 0))

    def test_model_matching_links_same_product_written_differently(self):
        products = [
            {"sku": "B1", "name": "Блок питания 850W 1stPlayer NGDP Gold, 80 PLUS Gold, "
                                  "белый (HA-850BA4-WH)"},
            {"sku": "B2", "name": "Видеокарта GeForce RTX 5070 12GB MSI SHADOW 2X OC (G5070-12S2C)"},
        ]
        competitors = [
            {"sku": "777", "name": "Блок питания 850W 1stPlayer NGDP HA-850BA4-WH",
             "supplier": "Прайс 482", "price_type": "retail", "price": "8520", "url": ""},
            {"sku": "778", "name": "Видеокарта GeForce RTX 5070 12Gb MSI Shadow 2X",
             "supplier": "Прайс 482", "price_type": "retail", "price": "94900", "url": ""},
        ]
        linked, by_sku, by_name = link_competitors(products, competitors)
        self.assertEqual((by_sku, by_name), (0, 2))
        self.assertEqual({row["sku"] for row in linked}, {"B1", "B2"})
        self.assertTrue(all("по модели" in row["supplier"] for row in linked))


class MenuKeyboardTest(unittest.TestCase):
    def test_main_menu_is_inline_with_named_actions(self):
        from aiogram.types import InlineKeyboardMarkup

        from app.keyboards import main_keyboard

        class FakeDb:
            def db_categories(self):
                return []

        keyboard = main_keyboard(FakeDb(), 0)
        self.assertIsInstance(keyboard, InlineKeyboardMarkup)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("menu:search", callbacks)
        self.assertIn("menu:profile", callbacks)
        self.assertIn("menu:apply", callbacks)
        self.assertTrue(any(data and data.startswith("cat:") for data in callbacks))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.db_file = self.folder / "bot.sqlite3"
        self.registry = self.folder / "wholesalers.json"
        self.db = Database(self.db_file, self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    def csv_file(self, filename, header, rows):
        path = self.folder / filename
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def test_import_approval_restart_and_refresh(self):
        product = self.csv_file("products.csv",
                                ["sku", "name", "category", "subcategory", "retail_price", "wholesale_price"],
                                [["SKU001", "Тестовый SSD", "Жесткие диски и SSD", "SSD", "5 000,50", "4 700"]])
        comp = self.csv_file("other_prices.csv", ["sku", "supplier", "price_type", "price"],
                             [["SKU001", "Конкурент", "retail", "5 900"]])
        self.assertEqual(self.db.import_prices(product, comp), (1, 1))
        item = self.db.products("Жесткие диски и SSD", "SSD", 0)[0]
        self.assertEqual(item["retail_price"], 500050)
        self.assertEqual(len(self.db.comparisons("SKU001", "retail")), 1)
        self.db.save_user(456123, "Тест Тестов", "testuser")
        req, created = self.db.request_wholesale(456123)
        self.assertTrue(created)
        req_again, created = self.db.request_wholesale(456123)
        self.assertFalse(created)
        self.assertEqual(req["id"], req_again["id"])
        self.db.review_request(req["id"], True, 987)
        self.assertTrue(self.db.is_wholesaler(456123))
        self.assertIn("456123", json.loads(self.registry.read_text()))
        # Повторный импорт и новая инициализация НЕ затрагивают реестр.
        self.db.import_prices(product, comp)
        reloaded = Database(self.db_file, self.registry)
        self.assertTrue(reloaded.is_wholesaler(456123))
        self.assertEqual(reloaded.latest_request(456123)["status"], "approved")
        csv_export = self.folder / "wholesalers.csv"
        reloaded.export_wholesalers_csv(csv_export)
        self.assertIn("456123", csv_export.read_text(encoding="utf-8-sig"))
        self.assertTrue(reloaded.remove_wholesaler(456123))
        self.assertFalse(reloaded.is_wholesaler(456123))

    def test_482_visible_to_both_roles_with_379_prices_and_registry_preserved(self):
        product = self.csv_file("products.csv", ["sku", "name", "category", "subcategory", "retail_price", "wholesale_price"],
                                [["5779", "Процессор Intel Pentium G3260 OEM", "Процессоры", "Intel", "2100", "1950"]])
        comp = self.csv_file("other_prices.csv", ["sku", "supplier", "price_type", "price"],
                             [["5779", "Прайс 482", "retail", "1350"]])
        self.db.import_prices(product, comp)
        self.db.save_user(1234, "Тест")
        req, _ = self.db.request_wholesale(1234)
        self.db.review_request(req["id"], True, 987)
        self.assertEqual(self.db.comparisons("5779", "retail")[0]["price"], 135000)
        self.assertEqual(self.db.comparisons("5779", "wholesale")[0]["price"], 135000)
        self.db.import_prices(product, comp)
        self.assertTrue(Database(self.db_file, self.registry).is_wholesaler(1234))

    def test_failed_import_does_not_erase_catalog(self):
        product = self.csv_file("products.csv", ["sku", "name", "category", "subcategory", "retail_price", "wholesale_price"],
                                [["X", "Товар", "Разное", "Другие", "100", ""]])
        other = self.csv_file("other.csv", ["sku", "supplier", "price_type", "price"], [])
        self.db.import_prices(product, other)
        product.write_text("sku,name,category,subcategory,retail_price,wholesale_price\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.db.import_prices(product, other)
        self.assertIsNotNone(self.db.product(1))

    def test_live_sync_379_482_from_separate_spreadsheets(self):
        main_rows = [
            ["Артикул", "Наименование", "Розница", "Опт"],
            ["M379-A", "Процессор Intel Pentium G3260 OEM", "2100", "1950"],
            ["5969", "Процессор Intel Celeron G3900 OEM", "1600", "1400"],
        ]
        secondary_rows = [
            ["", "Комплектующие", "", ""],
            ["", "Процессоры", "", ""],
            ["5779", "Процессор Intel Pentium G3260 OEM", "1350", "4"],
            ["5969", "Процессор Intel Celeron G3900 OEM", "800", "20"],
            ["7893", "Процессор Intel Celeron G3930 BOX", "950", "5"],
        ]
        mapping = self.folder / "column_map.json"
        mapping.write_text('{"header_row":1,"columns":{}}', encoding="utf-8")
        with patch.object(config, "DATA_DIR", self.folder), \
             patch.object(config, "PRODUCTS_PATH", self.folder / "products.csv"), \
             patch.object(config, "OTHER_PRICES_PATH", self.folder / "other_prices.csv"), \
             patch.object(config, "COLUMN_MAP_PATH", mapping), \
             patch.object(config, "GOOGLE_OTHER_GID", "0"), \
             patch.object(config, "GOOGLE_OTHER_SHEET_ID", "1aI1SuXZ2PfiKL4d4neY0xYRuWSHybS0PD1z3rFOnnM8"):
            sync = GoogleSheetSync(self.db)
            def fetch(sid, gid):
                return secondary_rows if gid == "0" else main_rows
            with patch.object(sync, "_get_rows", side_effect=fetch) as getter:
                count, competitors, warnings = sync.refresh()
            self.assertEqual((count, competitors), (2, 2))
            self.assertEqual(getter.call_count, 2)
            self.assertEqual(self.db.comparisons("M379-A", "retail")[0]["price"], 135000)
            self.assertEqual(self.db.comparisons("M379-A", "wholesale")[0]["price"], 135000)
            self.assertEqual(self.db.comparisons("5969", "retail")[0]["price"], 80000)
            self.assertEqual(warnings, [])

    def test_sheet_refresh_with_mock_data(self):
        mapping = self.folder / "column_map.json"
        mapping.write_text('{"header_row":1,"columns":{}}', encoding="utf-8")
        main_rows = [["Артикул", "Название", "Розница", "Опт"], ["A1", "Ryzen 5 7600", "15000", "14200"]]
        with patch.object(config, "DATA_DIR", self.folder), \
             patch.object(config, "PRODUCTS_PATH", self.folder / "products.csv"), \
             patch.object(config, "OTHER_PRICES_PATH", self.folder / "other_prices.csv"), \
             patch.object(config, "COLUMN_MAP_PATH", mapping), \
             patch.object(config, "GOOGLE_OTHER_GID", ""):
            sync = GoogleSheetSync(self.db)
            with patch.object(sync, "_get_rows", return_value=main_rows):
                count, others, warnings = sync.refresh()
            self.assertEqual((count, others), (1, 0))
            self.assertEqual(self.db.db_categories(), ["Процессоры"])
            self.assertTrue((self.folder / "products.csv").exists())
            self.assertTrue((self.folder / "other_prices.csv").exists())
            # Сетевая ошибка не удаляет сохранённый каталог.
            with patch.object(sync, "_get_rows", side_effect=ConnectionError("offline")):
                with self.assertRaises(ConnectionError):
                    sync.refresh()
            self.assertEqual(len(self.db.db_categories()), 1)


if __name__ == "__main__":
    unittest.main()
