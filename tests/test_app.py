import os
import shutil
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
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
        self.old_backup_time = app.BACKUP_TIME
        app.DB_PATH = Path(self.temp.name) / "test.db"
        app.TOKEN_PATH = Path(self.temp.name) / "token"
        app.BACKUP_DIR = Path(self.temp.name) / "backup"
        app.BACKUP_TIME = "00:00"
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.old_db
        app.TOKEN_PATH = self.old_token_path
        app.BACKUP_DIR = self.old_backup_dir
        app.BACKUP_TIME = self.old_backup_time
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
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 2)
            failure = conn.execute("SELECT status,error FROM imports ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(failure["status"], "failed")
            self.assertTrue(failure["error"])

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
        self.assertIn("weekly", result["analytics"]["trends"])
        self.assertIn("goal_metrics", result["analytics"])
        self.assertIn("data_quality", result["analytics"])
        self.assertEqual(result["analytics"]["data_quality"]["habits"][0]["coverage"], 100.0)
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

    def test_averages_ignore_the_running_period(self):
        # The current day has already reached its goal, but its quantity can
        # still grow and must not affect average/minimum until the day closes.
        completed_today = CSV.replace(
            b"2026-08-11,Fiber,,No,Daily,Building,25,10,g,Incomplete",
            b"2026-08-11,Fiber,,No,Daily,Building,25,30,g,Complete",
        )
        app.import_csv(completed_today, "test")
        detail = app.habit_detail("Fiber", {}, today=date(2026, 8, 11))
        self.assertEqual(detail["records"][-1]["state"], "complete")
        self.assertEqual((detail["average"], detail["minimum"], detail["maximum"]),
                         (30.0, 30.0, 30.0))
        habit = next(h for h in app.dashboard({}, today=date(2026, 8, 11))["habits"]
                     if h["name"] == "Fiber")
        self.assertEqual((habit["average"], habit["latest"]), (30.0, 30.0))

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

    def test_scheduled_backup_runs_once_after_configured_time(self):
        app.BACKUP_TIME = "23:59"
        app.import_csv(CSV, "test")
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        self.assertIsNone(app.backup_if_due(now))
        app.BACKUP_TIME = "00:00"
        first = app.backup_if_due(now)
        self.assertIsNotNone(first)
        self.assertIsNone(app.backup_if_due(now))

    def test_import_history_is_paginated_and_marks_unchanged_data(self):
        app.import_csv(CSV, "webhook", "first.csv")
        app.import_csv(CSV, "webhook", "second.csv")
        history = app.import_history({"page": ["1"], "per_page": ["1"]})
        self.assertEqual(history["pagination"]["total"], 2)
        self.assertEqual(history["items"][0]["source"], "webhook")
        self.assertEqual(history["items"][0]["changed"], 0)

    def test_backup_is_single_file_without_wal_sidecars(self):
        app.import_csv(CSV, "test")
        backup = app.backup_database("manual")
        with closing(app.sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse(Path(str(backup) + "-wal").exists())
        self.assertFalse(Path(str(backup) + "-shm").exists())

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

    def test_backup_with_leftover_wal_keeps_committed_rows(self):
        app.import_csv(CSV, "test")
        backup = app.backup_database("manual")
        conn = app.sqlite3.connect(backup)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""INSERT INTO records(date,name,description,archived,period,habit_type,goal,
                     quantity,unit,status,list_name,note,import_id)
                     SELECT date,'Extra',description,archived,period,habit_type,goal,quantity,unit,
                     status,list_name,note,import_id FROM records LIMIT 1""")
        conn.commit()
        stale = app.BACKUP_DIR / app.backup_filename("manual")
        shutil.copy(backup, stale)
        shutil.copy(str(backup) + "-wal", str(stale) + "-wal")
        conn.close()

        self.assertEqual(app.validate_database(stale)["counts"]["records"], 6)
        app.absorb_backup_sidecars()
        self.assertFalse(Path(str(stale) + "-wal").exists())
        self.assertEqual(app.validate_database(stale)["counts"]["records"], 6)
        app.restore_database(stale)
        with closing(app.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 6)

    def test_changed_import_is_preceded_by_backup_of_previous_snapshot(self):
        app.import_csv(CSV, "test")
        self.assertIsNone(app.import_csv(CSV, "test")["pre_import_backup"])

        changed = CSV.replace(b"2026-08-11,Fiber", b"2026-08-12,Fiber")
        pre_import = app.import_csv(changed, "test")["pre_import_backup"]
        self.assertIsNotNone(pre_import)
        backup = app.BACKUP_DIR / pre_import
        app.restore_database(backup)
        with closing(app.connect()) as conn:
            dates = {row[0] for row in conn.execute("SELECT date FROM records WHERE name='Fiber'")}
        self.assertIn("2026-08-11", dates)
        self.assertNotIn("2026-08-12", dates)

    def test_backup_time_and_validation_survive_mtime_rewrite(self):
        app.import_csv(CSV, "test")
        backup = app.backup_database("manual")
        self.assertIs(app.validate_backup(backup), app.validate_backup(backup))
        os.utime(backup, (0, 0))
        self.assertTrue(app.list_backups()[0]["modified"].startswith(date.today().isoformat()))

    def test_out_of_range_page_is_clamped(self):
        app.import_csv(CSV, "test")
        history = app.import_history({"page": ["9"], "per_page": ["10"]})
        self.assertEqual(history["pagination"]["page"], 1)
        self.assertFalse(history["pagination"]["has_previous"])
        self.assertTrue(history["items"])
        backups = app.backup_status({"page": ["9"], "per_page": ["10"]})
        self.assertEqual(backups["pagination"]["page"], 1)
        self.assertTrue(backups["backups"])

    def test_failed_imports_are_capped(self):
        original = app.FAILED_IMPORT_KEEP
        app.FAILED_IMPORT_KEEP = 3
        try:
            for _ in range(5):
                with self.assertRaises(app.ImportError):
                    app.import_csv(b"nie csv", "interfejs")
        finally:
            app.FAILED_IMPORT_KEEP = original
        with closing(app.connect()) as conn:
            failed = conn.execute("SELECT COUNT(*) FROM imports WHERE status='failed'").fetchone()[0]
        self.assertEqual(failed, 3)

    def test_multipart_extraction(self):
        body = (b"--abc\r\nContent-Disposition: form-data; name=\"file\"; filename=\"data.csv\"\r\n"
                b"Content-Type: text/csv\r\n\r\nhello\r\n--abc--\r\n")
        payload, filename = app.extract_multipart(body, "multipart/form-data; boundary=abc")
        self.assertEqual(payload, b"hello")
        self.assertEqual(filename, "data.csv")


if __name__ == "__main__":
    unittest.main()
