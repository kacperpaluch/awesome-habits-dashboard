import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

import app


CSV = """Date,Name,Description,Archived,Period,Type,Goal,Quantity,Unit,Status,Lists,Note
2026-08-10,Fiber,,No,Daily,Building,25,30,g,Complete,Health,
2026-08-11,Fiber,,No,Daily,Building,25,10,g,Incomplete,Health,
2026-08-10,No soda,,No,Daily,Breaking,0,0,,Complete,Health,
2026-08-11,No soda,,No,Daily,Breaking,0,1,,Incomplete,Health,
2026-08-10,Gym,,No,Weekly,Building,3,2,,Incomplete,Fitness,
""".encode()


class AwesomeHabitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = app.DB_PATH
        self.old_token_path = app.TOKEN_PATH
        self.old_backup_dir = app.BACKUP_DIR
        app.DB_PATH = Path(self.temp.name) / "test.db"
        app.TOKEN_PATH = Path(self.temp.name) / "token"
        app.BACKUP_DIR = Path(self.temp.name) / "backup"
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.old_db
        app.TOKEN_PATH = self.old_token_path
        app.BACKUP_DIR = self.old_backup_dir
        self.temp.cleanup()

    def test_parse_and_import_snapshot(self):
        result = app.import_csv(CSV, "test", "export.csv")
        self.assertEqual(result["rows"], 5)
        self.assertEqual(result["habits"], 3)
        with closing(app.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT filename FROM imports").fetchone()[0], "export.csv")

        replacement = CSV.replace(b"2026-08-11,Fiber", b"2026-08-12,Fiber")
        app.import_csv(replacement, "webhook")
        with closing(app.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 2)

    def test_invalid_import_preserves_previous_snapshot(self):
        app.import_csv(CSV, "test")
        with self.assertRaises(app.ImportError):
            app.import_csv(b"Date,Name\n2026-08-11,Fiber\n", "test")
        with closing(app.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 1)

    def test_dashboard_metrics_filters_and_streaks(self):
        app.import_csv(CSV, "test")
        result = app.dashboard({"start": ["2026-08-10"], "end": ["2026-08-11"]}, today=date(2026, 8, 11))
        self.assertEqual(result["summary"], {"done": 2, "missed": 0, "in_progress": 3,
                                             "records": 5, "resolved": 2,
                                             "rate": 100.0, "perfect_days": 1})
        fiber = next(item for item in result["habits"] if item["name"] == "Fiber")
        self.assertEqual((fiber["current_streak"], fiber["longest_streak"]), (1, 1))
        self.assertEqual((fiber["done"], fiber["missed"], fiber["in_progress"]), (1, 0, 1))
        gym = next(item for item in result["habits"] if item["name"] == "Gym")
        self.assertEqual((gym["missed"], gym["in_progress"]), (0, 1))
        self.assertNotIn("2026-08-11", {item["date"] for item in result["analytics"]["trends"]["daily"]})
        health = app.dashboard({"list": ["Health"]}, today=date(2026, 8, 11))
        self.assertEqual(health["summary"]["records"], 4)

    def test_incomplete_period_becomes_missed_after_it_ends(self):
        app.import_csv(CSV, "test")
        result = app.dashboard({}, today=date(2026, 8, 17))
        self.assertEqual(result["summary"]["in_progress"], 0)
        self.assertEqual(result["summary"]["missed"], 3)
        self.assertEqual(result["summary"]["rate"], 40.0)

    def test_range_with_only_current_records_has_no_failure_rate(self):
        app.import_csv(CSV, "test")
        result = app.dashboard({"start": ["2026-08-11"], "end": ["2026-08-11"]},
                               today=date(2026, 8, 11))
        self.assertEqual(result["summary"]["in_progress"], 2)
        self.assertEqual(result["summary"]["missed"], 0)
        self.assertIsNone(result["summary"]["rate"])

    def test_backup_is_valid_and_restore_creates_safety_copy(self):
        first = app.import_csv(CSV, "test")
        self.assertTrue(first["backup"])
        snapshot = app.backup_database("manual")
        validation = app.validate_database(snapshot)
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["counts"]["records"], 5)

        changed = CSV.replace(b"2026-08-11,Fiber", b"2026-08-12,Fiber")
        app.import_csv(changed, "test")
        restored = app.restore_database(snapshot)
        self.assertTrue(restored["ok"])
        self.assertTrue((app.BACKUP_DIR / restored["safety_backup"]).is_file())
        with closing(app.connect()) as conn:
            dates = {row[0] for row in conn.execute("SELECT date FROM records WHERE name='Fiber'")}
        self.assertIn("2026-08-11", dates)
        self.assertNotIn("2026-08-12", dates)

    def test_restore_rejects_foreign_file_without_changing_data(self):
        app.import_csv(CSV, "test")
        foreign = Path(self.temp.name) / "foreign.db"
        foreign.write_bytes(b"not sqlite")
        with self.assertRaises(app.ImportError):
            app.restore_database(foreign)
        with closing(app.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 5)

    def test_utf8_bom_and_decimal_comma_are_supported(self):
        csv_data = ('\ufeff"Date","Name","Period","Type","Goal","Quantity","Status"\n'
                    '"2026-08-11","Błonnik","Daily","Building","25","10,5","Incomplete"\n').encode()
        rows = app.parse_csv(csv_data)
        self.assertEqual(rows[0]["quantity"], 10.5)

    def test_multipart_extraction(self):
        body = (b"--abc\r\nContent-Disposition: form-data; name=\"file\"; filename=\"data.csv\"\r\n"
                b"Content-Type: text/csv\r\n\r\nhello\r\n--abc--\r\n")
        payload, filename = app.extract_multipart(body, "multipart/form-data; boundary=abc")
        self.assertEqual(payload, b"hello")
        self.assertEqual(filename, "data.csv")


if __name__ == "__main__":
    unittest.main()
