#!/usr/bin/env python3
"""Awesome Habits Lens — dependency-free CSV analytics dashboard."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import sqlite3
import statistics
import sys
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def load_local_env() -> None:
    path = BASE_DIR / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value.strip().strip("'\""))


load_local_env()

DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "habits.db")))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
MAX_UPLOAD_BYTES = max(1024, int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024)
MAX_BACKUP_BYTES = max(1024, int(os.getenv("MAX_BACKUP_MB", "100")) * 1024 * 1024)
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
TOKEN_PATH = Path(os.getenv("WEBHOOK_TOKEN_FILE", str(DB_PATH.parent / "webhook-token")))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(DB_PATH.parent / "backup")))
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP", "14")))
BACKUP_TIME = os.getenv("BACKUP_TIME", "03:00").strip()
FAILED_IMPORT_KEEP = 50
IMPORT_LOCK = threading.Lock()
DB_LOCK = threading.RLock()

REQUIRED_COLUMNS = {"Date", "Name", "Period", "Type", "Goal", "Quantity", "Status"}
ALLOWED_PERIODS = {"Daily", "Weekly"}
ALLOWED_TYPES = {"Building", "Breaking"}
ALLOWED_STATUSES = {"Complete", "Incomplete"}


class ImportError(ValueError):
    pass


def webhook_token() -> str:
    global WEBHOOK_TOKEN
    if WEBHOOK_TOKEN:
        return WEBHOOK_TOKEN
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.is_file():
        WEBHOOK_TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not WEBHOOK_TOKEN:
        WEBHOOK_TOKEN = secrets.token_urlsafe(24)
        TOKEN_PATH.write_text(WEBHOOK_TOKEN + "\n", encoding="utf-8")
        try:
            TOKEN_PATH.chmod(0o600)
        except OSError:
            pass
    return WEBHOOK_TOKEN


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def database():
    with DB_LOCK:
        conn = connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                imported_at TEXT NOT NULL,
                source TEXT NOT NULL,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                rows_count INTEGER NOT NULL,
                min_date TEXT,
                max_date TEXT,
                status TEXT NOT NULL DEFAULT 'success',
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                period TEXT NOT NULL,
                habit_type TEXT NOT NULL,
                goal REAL NOT NULL DEFAULT 0,
                quantity REAL NOT NULL DEFAULT 0,
                unit TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                list_name TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                import_id INTEGER NOT NULL REFERENCES imports(id)
            );
            CREATE INDEX IF NOT EXISTS idx_records_date ON records(date);
            CREATE INDEX IF NOT EXISTS idx_records_name_date ON records(name, date);
            PRAGMA user_version=1;
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(imports)")}
        if "status" not in columns:
            conn.execute("ALTER TABLE imports ADD COLUMN status TEXT NOT NULL DEFAULT 'success'")
        if "error" not in columns:
            conn.execute("ALTER TABLE imports ADD COLUMN error TEXT")


def validate_database(path: Path) -> dict:
    """Validate an Awesome Habits Lens SQLite backup before it is trusted."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Plik nie istnieje lub jest pusty")
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise ValueError("Plik nie jest bazą SQLite")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"integrity_check: {integrity}")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            missing = sorted({"imports", "records"} - tables)
            if missing:
                raise ValueError("Brak tabel Awesome Habits Lens: " + ", ".join(missing))
            counts = {
                "imports": conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0],
                "records": conn.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            }
        finally:
            conn.close()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"valid": True, "error": None, "integrity": "ok", "counts": counts,
                "size_kb": round(path.stat().st_size / 1024, 1), "sha256": digest.hexdigest()}
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        return {"valid": False, "error": str(exc), "integrity": None, "counts": None,
                "size_kb": round(path.stat().st_size / 1024, 1) if path.exists() else 0,
                "sha256": None}


@lru_cache(maxsize=32)
def cached_validation(path: str, fingerprint: tuple[int, int]) -> dict:
    return validate_database(Path(path))


def validate_backup(path: Path) -> dict:
    """Validate a backup file, reusing the result while the file stays untouched."""
    stat = path.stat()
    return cached_validation(str(path), (stat.st_mtime_ns, stat.st_size))


def backup_filename(kind: str = "backup") -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    return f"awesome-habits-{kind}-{stamp}.db"


def backup_moment(path: Path) -> datetime:
    """Creation time taken from the filename, which survives copying and volume restores."""
    try:
        stamp = "-".join(path.stem.split("-")[-3:])
        return datetime.strptime(stamp, "%Y-%m-%d_%H%M%S_%f").astimezone()
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def cleanup_backup_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def absorb_wal(path: Path) -> None:
    """Fold a leftover -wal into the database file; deleting it would lose committed data."""
    if not Path(str(path) + "-wal").exists():
        return
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()


def absorb_backup_sidecars() -> None:
    if not BACKUP_DIR.is_dir():
        return
    for path in BACKUP_DIR.glob("awesome-habits-*.db"):
        try:
            absorb_wal(path)
        except sqlite3.DatabaseError as exc:
            print(f"Nie udało się scalić WAL backupu {path.name}: {exc}", flush=True)
            continue
        cleanup_backup_sidecars(path)


def backup_clock() -> tuple[int, int]:
    try:
        parsed = datetime.strptime(BACKUP_TIME, "%H:%M")
    except ValueError as exc:
        raise ImportError("BACKUP_TIME musi mieć format HH:MM, np. 03:00") from exc
    return parsed.hour, parsed.minute


def prune_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("awesome-habits-*.db"), key=backup_moment)
    for old in backups[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
        cleanup_backup_sidecars(old)


def backup_database(kind: str = "backup", *, prune: bool = True) -> Path:
    """Create and verify a consistent online SQLite backup."""
    with DB_LOCK:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        target = BACKUP_DIR / backup_filename(kind)
        source = connect()
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()
            source.close()
        cleanup_backup_sidecars(target)
        validation = validate_database(target)
        if not validation["valid"]:
            target.unlink(missing_ok=True)
            raise ImportError(f"Backup nie przeszedł kontroli: {validation['error']}")
        if prune:
            prune_backups()
        return target


def resolve_backup(filename: str) -> Path:
    if (not filename or Path(filename).name != filename
            or not filename.startswith("awesome-habits-") or not filename.endswith(".db")):
        raise ImportError("Nieprawidłowa nazwa backupu")
    path = BACKUP_DIR / filename
    if not path.is_file():
        raise ImportError("Nie znaleziono backupu")
    return path


def list_backups() -> list[dict]:
    if not BACKUP_DIR.is_dir():
        return []
    result = []
    for path in sorted(BACKUP_DIR.glob("awesome-habits-*.db"), key=backup_moment, reverse=True):
        stat = path.stat()
        result.append({
            "file": path.name, "size_kb": round(stat.st_size / 1024, 1),
            "modified": backup_moment(path).isoformat(timespec="seconds"),
            "kind": ("pre_restore" if "-pre-restore-" in path.name else
                     "pre_import" if "-pre-import-" in path.name else
                     "manual" if "-manual-" in path.name else
                     "scheduled" if "-scheduled-" in path.name else "snapshot"),
        })
    return result


def history_options(params: dict[str, list[str]]) -> tuple[int, int, str | None, str | None]:
    try:
        page = max(1, int(params.get("page", ["1"])[0]))
        per_page = min(50, max(1, int(params.get("per_page", ["10"])[0])))
    except ValueError as exc:
        raise ImportError("Nieprawidłowa strona historii") from exc
    date_from = params.get("date_from", [""])[0] or None
    date_to = params.get("date_to", [""])[0] or None
    try:
        if date_from:
            date.fromisoformat(date_from)
        if date_to:
            date.fromisoformat(date_to)
    except ValueError as exc:
        raise ImportError("Daty historii muszą mieć format RRRR-MM-DD") from exc
    if date_from and date_to and date_from > date_to:
        raise ImportError("Data początkowa nie może być późniejsza niż końcowa")
    return page, per_page, date_from, date_to


def page_count(total: int, per_page: int) -> int:
    return max(1, (total + per_page - 1) // per_page)


def page_result(items: list[dict], total: int, page: int, per_page: int) -> dict:
    pages = page_count(total, per_page)
    page = min(page, pages)
    return {"items": items, "pagination": {"page": page, "per_page": per_page,
            "total": total, "pages": pages, "has_previous": page > 1,
            "has_next": page < pages}}


def backup_status(params: dict[str, list[str]] | None = None) -> dict:
    backups = list_backups()
    latest = backups[0] if backups else None
    validation = validate_backup(resolve_backup(latest["file"])) if latest else None
    page, per_page, date_from, date_to = history_options(params or {})
    filtered = [item for item in backups
                if (not date_from or item["modified"][:10] >= date_from)
                and (not date_to or item["modified"][:10] <= date_to)]
    page = min(page, page_count(len(filtered), per_page))
    offset = (page - 1) * per_page
    return {"healthy": bool(latest and validation and validation["valid"]),
            "keep": BACKUP_KEEP, "backup_time": BACKUP_TIME,
            "latest": latest, "latest_validation": validation,
            "backups": filtered[offset:offset + per_page],
            "pagination": page_result([], len(filtered), page, per_page)["pagination"]}


def import_history(params: dict[str, list[str]]) -> dict:
    page, per_page, date_from, date_to = history_options(params)
    clauses, values = [], []
    if date_from:
        clauses.append("substr(imported_at,1,10) >= ?")
        values.append(date_from)
    if date_to:
        clauses.append("substr(imported_at,1,10) <= ?")
        values.append(date_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with database() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM imports{where}", values).fetchone()[0]
        page = min(page, page_count(total, per_page))
        offset = (page - 1) * per_page
        rows = [dict(row) for row in conn.execute(
            f"""SELECT current.*,
                CASE WHEN current.status != 'success' THEN NULL
                    WHEN current.sha256 = (SELECT previous.sha256 FROM imports previous
                    WHERE previous.id < current.id AND previous.status = 'success'
                    ORDER BY previous.id DESC LIMIT 1) THEN 0 ELSE 1 END AS changed
                FROM imports current{where} ORDER BY current.id DESC LIMIT ? OFFSET ?""",
            [*values, per_page, offset])]
    return page_result(rows, total, page, per_page)


def backup_if_due(now: datetime | None = None) -> Path | None:
    with DB_LOCK:
        now = now or datetime.now().astimezone()
        hour, minute = backup_clock()
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < due:
            return None
        for item in list_backups():
            if item["kind"] != "scheduled":
                continue
            modified = datetime.fromisoformat(item["modified"])
            if modified.date() == now.date():
                return None
        with database() as conn:
            if conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0:
                return None
        return backup_database("scheduled")


def restore_database(source_path: Path) -> dict:
    """Validate, safety-backup, and restore the active database."""
    with DB_LOCK:
        try:
            absorb_wal(source_path)
        except sqlite3.DatabaseError as exc:
            raise ImportError(f"Nie można przywrócić backupu: {exc}") from exc
        validation = validate_database(source_path)
        if not validation["valid"]:
            raise ImportError(f"Nie można przywrócić backupu: {validation['error']}")
        safety = backup_database("pre-restore", prune=False)
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        destination = connect()
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
            cleanup_backup_sidecars(source_path)
        init_db()
        restored = validate_database(DB_PATH)
        if not restored["valid"]:
            raise ImportError(f"Przywrócona baza nie przeszła kontroli: {restored['error']}")
        prune_backups()
        return {"ok": True, "restored_from": source_path.name,
                "safety_backup": safety.name, "validation": restored}


def text_value(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "").strip()


def number_value(row: dict[str, str], key: str, line: int) -> float:
    raw = text_value(row, key).replace(",", ".")
    try:
        return float(raw or 0)
    except ValueError as exc:
        raise ImportError(f"Wiersz {line}: pole {key} nie jest liczbą ({raw!r})") from exc


def parse_csv(payload: bytes) -> list[dict]:
    if not payload:
        raise ImportError("Plik jest pusty")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ImportError(f"Plik przekracza limit {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportError("Plik musi być zapisany jako UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise ImportError("Brak wymaganych kolumn: " + ", ".join(missing))
    result = []
    for line, row in enumerate(reader, 2):
        if not any(text_value(row, key) for key in columns):
            continue
        raw_date = text_value(row, "Date")
        try:
            date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ImportError(f"Wiersz {line}: nieprawidłowa data {raw_date!r}") from exc
        name = text_value(row, "Name")
        if not name:
            raise ImportError(f"Wiersz {line}: brak nazwy nawyku")
        period = text_value(row, "Period").title()
        habit_type = text_value(row, "Type").title()
        status = text_value(row, "Status").title()
        if period not in ALLOWED_PERIODS:
            raise ImportError(f"Wiersz {line}: nieobsługiwany okres {period!r}")
        if habit_type not in ALLOWED_TYPES:
            raise ImportError(f"Wiersz {line}: nieobsługiwany typ {habit_type!r}")
        if status not in ALLOWED_STATUSES:
            raise ImportError(f"Wiersz {line}: nieobsługiwany status {status!r}")
        result.append({
            "date": raw_date,
            "name": name,
            "description": text_value(row, "Description"),
            "archived": int(text_value(row, "Archived").lower() in {"yes", "true", "1"}),
            "period": period,
            "habit_type": habit_type,
            "goal": number_value(row, "Goal", line),
            "quantity": number_value(row, "Quantity", line),
            "unit": text_value(row, "Unit"),
            "status": status,
            "list_name": text_value(row, "Lists"),
            "note": text_value(row, "Note"),
        })
    if not result:
        raise ImportError("Plik nie zawiera żadnych rekordów")
    return result


def import_csv(payload: bytes, source: str, filename: str = "AwesomeHabits.csv") -> dict:
    digest = hashlib.sha256(payload).hexdigest()
    imported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        rows = parse_csv(payload)
    except ImportError as exc:
        with database() as conn:
            conn.execute(
                """INSERT INTO imports(imported_at,source,filename,sha256,rows_count,
                min_date,max_date,status,error) VALUES(?,?,?,?,0,NULL,NULL,'failed',?)""",
                (imported_at, source, filename[:255], digest, str(exc)[:500]),
            )
            # /api/import przyjmuje zgłoszenia bez tokenu, więc historia błędów ma twardy limit
            conn.execute(
                """DELETE FROM imports WHERE status='failed' AND id NOT IN
                (SELECT id FROM imports WHERE status='failed' ORDER BY id DESC LIMIT ?)""",
                (FAILED_IMPORT_KEEP,),
            )
        raise
    if not IMPORT_LOCK.acquire(blocking=False):
        raise ImportError("Inny import jest już w toku")
    try:
        with database() as conn:
            previous = conn.execute(
                "SELECT sha256 FROM imports WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        # Import podmienia cały snapshot, więc poprzedni stan zapisujemy zanim zniknie.
        unchanged = bool(previous and previous["sha256"] == digest)
        pre_import = backup_database("pre-import").name if previous and not unchanged else None
        with database() as conn:
            cur = conn.execute(
                "INSERT INTO imports(imported_at,source,filename,sha256,rows_count,min_date,max_date) VALUES(?,?,?,?,?,?,?)",
                (imported_at, source, filename[:255], digest, len(rows),
                 min(r["date"] for r in rows), max(r["date"] for r in rows)),
            )
            import_id = cur.lastrowid
            conn.execute("DELETE FROM records")
            conn.executemany(
                """INSERT INTO records(date,name,description,archived,period,habit_type,goal,
                quantity,unit,status,list_name,note,import_id) VALUES
                (:date,:name,:description,:archived,:period,:habit_type,:goal,:quantity,
                :unit,:status,:list_name,:note,:import_id)""",
                [{**row, "import_id": import_id} for row in rows],
            )
        backup_name = None
        backup_error = None
        try:
            scheduled = backup_if_due()
            backup_name = scheduled.name if scheduled else None
        except (ImportError, OSError, sqlite3.Error) as exc:
            backup_error = str(exc)
        return {
            "ok": True, "import_id": import_id, "rows": len(rows),
            "habits": len({r["name"] for r in rows}), "min_date": min(r["date"] for r in rows),
            "max_date": max(r["date"] for r in rows),
            "unchanged": unchanged, "pre_import_backup": pre_import,
            "backup": backup_name, "backup_error": backup_error,
        }
    finally:
        IMPORT_LOCK.release()


def is_complete(row: dict | sqlite3.Row) -> bool:
    return str(row["status"]).lower() == "complete"


def period_key(day: date, period: str) -> date:
    return day - timedelta(days=day.weekday()) if period.lower() == "weekly" else day


def record_state(row: dict | sqlite3.Row, today: date) -> str:
    """Return a final or provisional state for a daily/weekly record."""
    if is_complete(row):
        return "complete"
    current_period = period_key(today, row["period"]).isoformat()
    return "in_progress" if row["date"] == current_period else "missed"


def streaks(rows: list[dict], today: date) -> tuple[int, int, str]:
    if not rows:
        return 0, 0, "day"
    weekly = rows[0]["period"].lower() == "weekly"
    unit, step = ("week", timedelta(weeks=1)) if weekly else ("day", timedelta(days=1))
    values = {period_key(date.fromisoformat(r["date"]), r["period"]): is_complete(r) for r in rows}
    completed = sorted(key for key, done in values.items() if done)
    best = run = 0
    previous = None
    for key in completed:
        run = run + 1 if previous is not None and key - previous == step else 1
        best = max(best, run)
        previous = key
    cursor = period_key(today, rows[0]["period"])
    if not values.get(cursor, False):
        cursor -= step
    current = 0
    while values.get(cursor, False):
        current += 1
        cursor -= step
    return current, best, unit


def query_records(params: dict[str, list[str]], include_archived: bool = False) -> list[dict]:
    clauses, args = [], []
    if not include_archived:
        clauses.append("archived=0")
    if params.get("start"):
        clauses.append("date>=?")
        args.append(params["start"][0])
    if params.get("end"):
        clauses.append("date<=?")
        args.append(params["end"][0])
    for field, column in (("habit", "name"), ("list", "list_name"), ("period", "period")):
        values = [v for v in params.get(field, []) if v]
        if values:
            clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
            args.extend(values)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with database() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM records" + where + " ORDER BY date,name", args)]


def rate_of(rows: list[dict], today: date | None = None) -> float | None:
    today = today or date.today()
    resolved = [row for row in rows if record_state(row, today) != "in_progress"]
    return round(sum(is_complete(r) for r in resolved) / len(resolved) * 100, 1) if resolved else None


def streak_rows(all_rows: list[dict], name: str, period: str) -> list[dict]:
    return [r for r in all_rows if r["name"] == name and r["period"] == period]


def dashboard(params: dict[str, list[str]], today: date | None = None) -> dict:
    today = today or date.today()
    rows = query_records(params)
    all_rows = query_records({k: v for k, v in params.items() if k not in {"start", "end"}})
    grouped, days = defaultdict(list), defaultdict(list)
    for row in rows:
        grouped[(row["name"], row["period"])].append(row)
        if row["period"] == "Daily":
            days[row["date"]].append(row)
    habits = []
    for (name, period), items in grouped.items():
        history = streak_rows(all_rows, name, period)
        current, longest, unit = streaks(history, today)
        done = sum(is_complete(r) for r in items)
        in_progress = sum(record_state(r, today) == "in_progress" for r in items)
        missed = sum(record_state(r, today) == "missed" for r in items)
        quantities = [r["quantity"] for r in items]
        habits.append({
            "name": name, "period": period, "type": items[-1]["habit_type"],
            "unit": items[-1]["unit"], "goal": items[-1]["goal"], "list": items[-1]["list_name"],
            "done": done, "missed": missed, "in_progress": in_progress,
            "rate": rate_of(items, today),
            "current_streak": current, "longest_streak": longest, "streak_unit": unit,
            "average": round(statistics.fmean(quantities), 2), "latest": quantities[-1],
        })
    habits.sort(key=lambda h: (h["rate"] is None, -(h["rate"] or 0), h["name"].lower()))
    done = sum(is_complete(r) for r in rows)
    missed = sum(record_state(r, today) == "missed" for r in rows)
    in_progress = sum(record_state(r, today) == "in_progress" for r in rows)
    total = len(rows)
    heatmap = [{"date": key, "done": sum(is_complete(r) for r in items),
                "missed": sum(record_state(r, today) == "missed" for r in items),
                "in_progress": sum(record_state(r, today) == "in_progress" for r in items),
                "total": len(items),
                "rate": round(sum(is_complete(r) for r in items) / len(items) * 100)}
               for key, items in sorted(days.items())]
    day_rates = []
    for item in heatmap:
        if item["in_progress"]:
            continue
        day_rates.append(item["rate"])
        item["avg7"] = round(statistics.fmean(day_rates[-7:]), 1)
        item["avg30"] = round(statistics.fmean(day_rates[-30:]), 1)
    trend = [item for item in heatmap if not item["in_progress"]]
    weekday_stats = []
    for weekday in range(7):
        items = [r for r in rows if r["period"] == "Daily" and date.fromisoformat(r["date"]).weekday() == weekday]
        weekday_stats.append({"day": weekday, "rate": rate_of(items, today), "records": len(items)})
    month_groups = defaultdict(list)
    for row in rows:
        if row["period"] == "Daily":
            month_groups[row["date"][:7]].append(row)
    monthly = []
    for month, items in sorted(month_groups.items()):
        month_days = defaultdict(list)
        for item in items:
            month_days[item["date"]].append(item)
        monthly.append({"month": month, "rate": rate_of(items, today), "records": len(items),
                        "perfect_days": sum(
                            all(record_state(r, today) == "complete" for r in group)
                            for group in month_days.values()
                        )})
    current_period = {}
    for row in all_rows:
        key = period_key(today, row["period"]).isoformat()
        if row["date"] == key:
            current_period[(row["name"], row["period"])] = row
    pending, today_done = [], 0
    for (name, period), row in current_period.items():
        if is_complete(row):
            today_done += 1
        else:
            current, _, unit = streaks(streak_rows(all_rows, name, period), today)
            pending.append({"name": name, "period": period, "type": row["habit_type"],
                            "streak": current, "unit": unit,
                            "quantity": row["quantity"], "goal": row["goal"], "value_unit": row["unit"]})
    with database() as conn:
        bounds = conn.execute("SELECT MIN(date) min_date,MAX(date) max_date FROM records WHERE archived=0").fetchone()
        options = conn.execute("SELECT DISTINCT name,list_name,period FROM records WHERE archived=0 ORDER BY name").fetchall()
        latest = conn.execute(
            "SELECT * FROM imports WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    week_groups = defaultdict(list)
    for row in rows:
        if row["period"] == "Daily":
            key = period_key(date.fromisoformat(row["date"]), "Weekly").isoformat()
            week_groups[key].append(row)
    current_week = period_key(today, "Weekly").isoformat()
    week_rates = [rate_of(items, today) for key, items in week_groups.items() if key != current_week]
    week_rates = [value for value in week_rates if value is not None]
    return {
        "summary": {"done": done, "missed": missed, "in_progress": in_progress,
                    "records": total, "resolved": done + missed,
                    "rate": round(done / (done + missed) * 100, 1) if done + missed else None,
                    "perfect_days": sum(
                        bool(items) and all(record_state(r, today) == "complete" for r in items)
                        for items in days.values()
                    )},
        "habits": habits, "heatmap": heatmap,
        "bounds": dict(bounds) if bounds else {"min_date": None, "max_date": None},
        "options": {"habits": sorted({r["name"] for r in options}),
                    "lists": sorted({r["list_name"] for r in options if r["list_name"]}),
                    "periods": sorted({r["period"] for r in options})},
        "analytics": {"trends": {"daily": trend}, "weekdays": weekday_stats, "monthly": monthly,
                      "regularity": {"weekly_stddev": round(statistics.pstdev(week_rates), 1) if len(week_rates) > 1 else None,
                                     "weeks": len(week_rates)},
                      "today": {"date": today.isoformat(), "done": today_done, "total": len(current_period),
                                "pending": sorted(pending, key=lambda x: x["name"].lower())},
                      "latest_import": dict(latest) if latest else None},
    }


def habit_detail(name: str, params: dict[str, list[str]], today: date | None = None) -> dict | None:
    today = today or date.today()
    selected = dict(params)
    selected["habit"] = [name]
    rows = query_records(selected)
    all_rows = query_records({"habit": [name]})
    if not all_rows:
        return None
    period = params.get("period", [all_rows[-1]["period"]])[0]
    rows = [r for r in rows if r["period"] == period]
    history = [r for r in all_rows if r["period"] == period]
    if not history:
        return None
    current, longest, unit = streaks(history, today)
    values = [r["quantity"] for r in rows]
    in_progress = sum(record_state(r, today) == "in_progress" for r in rows)
    return {"name": name, "period": period, "type": history[-1]["habit_type"],
            "goal": history[-1]["goal"], "unit": history[-1]["unit"], "list": history[-1]["list_name"],
            "current_streak": current, "longest_streak": longest, "streak_unit": unit,
            "done": sum(is_complete(r) for r in rows),
            "missed": sum(record_state(r, today) == "missed" for r in rows),
            "in_progress": in_progress, "rate": rate_of(rows, today),
            "average": round(statistics.fmean(values), 2) if values else 0,
            "minimum": min(values) if values else 0, "maximum": max(values) if values else 0,
            "records": [{"date": r["date"], "quantity": r["quantity"],
                         "state": record_state(r, today), "status": r["status"]} for r in rows]}


def extract_multipart(body: bytes, content_type: str) -> tuple[bytes, str]:
    marker = "boundary="
    if marker not in content_type:
        raise ImportError("Brak granicy multipart")
    boundary = content_type.split(marker, 1)[1].strip().strip('"').encode()
    for part in body.split(b"--" + boundary):
        head, separator, content = part.partition(b"\r\n\r\n")
        if separator and b'name="file"' in head:
            filename = "AwesomeHabits.csv"
            disposition = head.decode("utf-8", errors="replace").lstrip("\r\n").split("\r\n", 1)[0]
            for chunk in disposition.split(";"):
                if chunk.strip().startswith("filename="):
                    filename = chunk.split("=", 1)[1].strip().strip('"')
            if content.endswith(b"\r\n"):
                content = content[:-2]
            return content, Path(filename).name
    raise ImportError("Nie znaleziono pola file")


class Handler(BaseHTTPRequestHandler):
    server_version = "AwesomeHabitsLens/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # Never print the secret webhook URL from request logs.
        safe_path = urlparse(str(args[0]).split(" ")[1]).path if args else ""
        if safe_path.startswith("/webhook/"):
            args = tuple(str(arg).replace(safe_path, "/webhook/[redacted]") for arg in args)
        super().log_message(fmt, *args)

    def json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def file_response(self, path: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.sqlite3")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                self.wfile.write(chunk)

    def read_body(self, limit: int = MAX_UPLOAD_BYTES + 65536) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ImportError("Nieprawidłowy Content-Length") from exc
        if length <= 0:
            raise ImportError("Brak pliku w żądaniu")
        if length > limit:
            raise ImportError(f"Żądanie przekracza limit {limit // 1024 // 1024} MB")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                return self.json_response({"ok": True})
            if parsed.path == "/api/config":
                with database() as conn:
                    latest_event = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()
                    latest = conn.execute(
                        "SELECT * FROM imports WHERE status='success' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", f"localhost:{PORT}")
                proto = self.headers.get("X-Forwarded-Proto", "http")
                return self.json_response({"webhook_url": f"{proto}://{host}/webhook/{webhook_token()}",
                                           "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
                                           "latest_import": dict(latest) if latest else None,
                                           "latest_event": dict(latest_event) if latest_event else None})
            if parsed.path == "/api/dashboard":
                return self.json_response(dashboard(params))
            if parsed.path == "/api/backups":
                return self.json_response(backup_status(params))
            if parsed.path == "/api/imports":
                return self.json_response(import_history(params))
            if parsed.path.startswith("/api/backups/") and parsed.path.endswith("/download"):
                filename = unquote(parsed.path.removeprefix("/api/backups/").removesuffix("/download"))
                return self.file_response(resolve_backup(filename))
            if parsed.path.startswith("/api/habits/"):
                detail = habit_detail(unquote(parsed.path.removeprefix("/api/habits/")), params)
                return self.json_response(detail or {"error": "Nie znaleziono nawyku"}, 200 if detail else 404)
            return self.serve_static(parsed.path)
        except (ImportError, ValueError, sqlite3.Error) as exc:
            return self.json_response({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        is_ui = parsed.path == "/api/import"
        is_hook = parsed.path.startswith("/webhook/") and hmac.compare_digest(
            unquote(parsed.path.removeprefix("/webhook/")), webhook_token())
        try:
            if parsed.path == "/api/backup":
                path = backup_database("manual")
                return self.json_response({"ok": True, "backup": path.name,
                                           "validation": validate_database(path)}, HTTPStatus.CREATED)
            if parsed.path.startswith("/api/backups/") and parsed.path.endswith("/restore"):
                filename = unquote(parsed.path.removeprefix("/api/backups/").removesuffix("/restore"))
                payload = json.loads(self.read_body().decode("utf-8"))
                if payload.get("confirmation") != "PRZYWRÓĆ":
                    raise ImportError("Wymagane potwierdzenie PRZYWRÓĆ")
                return self.json_response(restore_database(resolve_backup(filename)))
            if parsed.path == "/api/backups/restore-upload":
                if params.get("confirmation", [""])[0] != "PRZYWRÓĆ":
                    raise ImportError("Wymagane potwierdzenie PRZYWRÓĆ")
                body = self.read_body(MAX_BACKUP_BYTES + 65536)
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data"):
                    raise ImportError("Backup należy wysłać jako multipart/form-data")
                payload, filename = extract_multipart(body, content_type)
                if len(payload) > MAX_BACKUP_BYTES:
                    raise ImportError(f"Backup przekracza limit {MAX_BACKUP_BYTES // 1024 // 1024} MB")
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                temporary = BACKUP_DIR / (".restore-upload-" + secrets.token_hex(8) + ".db")
                try:
                    temporary.write_bytes(payload)
                    result = restore_database(temporary)
                    result["restored_from"] = Path(filename).name
                    return self.json_response(result)
                finally:
                    temporary.unlink(missing_ok=True)
            if not is_ui and not is_hook:
                return self.json_response({"error": "Nie znaleziono endpointu"}, 404)
            body = self.read_body()
            content_type = self.headers.get("Content-Type", "")
            if content_type.startswith("multipart/form-data"):
                payload, filename = extract_multipart(body, content_type)
            else:
                payload, filename = body, self.headers.get("X-Filename", "AwesomeHabits.csv")
            result = import_csv(payload, "interfejs" if is_ui else "webhook", Path(filename).name)
            return self.json_response(result, HTTPStatus.CREATED)
        except (ImportError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (OSError, sqlite3.Error) as exc:
            return self.json_response({"error": f"Błąd bazy danych: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else unquote(path).lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self.json_response({"error": "Nie znaleziono"}, 404)
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def backup_loop() -> None:
    while True:
        try:
            created = backup_if_due()
            if created:
                print(f"Automatyczny backup: {created.name}", flush=True)
        except Exception as exc:
            print(f"Automatyczny backup nie powiódł się: {exc}", flush=True)
        threading.Event().wait(60)


def main() -> None:
    init_db()
    absorb_backup_sidecars()
    webhook_token()
    try:
        backup_if_due()
    except Exception as exc:
        print(f"Backup przy starcie nie powiódł się: {exc}", flush=True)
    threading.Thread(target=backup_loop, name="scheduled-backup", daemon=True).start()
    print(f"Awesome Habits Lens: http://{HOST}:{PORT}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
