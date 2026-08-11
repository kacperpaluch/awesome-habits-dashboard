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
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
TOKEN_PATH = Path(os.getenv("WEBHOOK_TOKEN_FILE", str(DB_PATH.parent / "webhook-token")))
IMPORT_LOCK = threading.Lock()

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
                max_date TEXT
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
    rows = parse_csv(payload)
    digest = hashlib.sha256(payload).hexdigest()
    imported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if not IMPORT_LOCK.acquire(blocking=False):
        raise ImportError("Inny import jest już w toku")
    try:
        with database() as conn:
            previous = conn.execute("SELECT sha256 FROM imports ORDER BY id DESC LIMIT 1").fetchone()
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
        return {
            "ok": True, "import_id": import_id, "rows": len(rows),
            "habits": len({r["name"] for r in rows}), "min_date": min(r["date"] for r in rows),
            "max_date": max(r["date"] for r in rows),
            "unchanged": bool(previous and previous["sha256"] == digest),
        }
    finally:
        IMPORT_LOCK.release()


def is_complete(row: dict | sqlite3.Row) -> bool:
    return str(row["status"]).lower() == "complete"


def period_key(day: date, period: str) -> date:
    return day - timedelta(days=day.weekday()) if period.lower() == "weekly" else day


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


def rate_of(rows: list[dict]) -> float | None:
    return round(sum(is_complete(r) for r in rows) / len(rows) * 100, 1) if rows else None


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
        quantities = [r["quantity"] for r in items]
        habits.append({
            "name": name, "period": period, "type": items[-1]["habit_type"],
            "unit": items[-1]["unit"], "goal": items[-1]["goal"], "list": items[-1]["list_name"],
            "done": done, "missed": len(items) - done, "rate": round(done / len(items) * 100, 1),
            "current_streak": current, "longest_streak": longest, "streak_unit": unit,
            "average": round(statistics.fmean(quantities), 2), "latest": quantities[-1],
        })
    habits.sort(key=lambda h: (-h["rate"], h["name"].lower()))
    total = len(rows)
    done = sum(is_complete(r) for r in rows)
    heatmap = [{"date": key, "done": sum(is_complete(r) for r in items), "total": len(items),
                "rate": round(sum(is_complete(r) for r in items) / len(items) * 100)}
               for key, items in sorted(days.items())]
    day_rates = []
    for item in heatmap:
        day_rates.append(item["rate"])
        item["avg7"] = round(statistics.fmean(day_rates[-7:]), 1)
        item["avg30"] = round(statistics.fmean(day_rates[-30:]), 1)
    weekday_stats = []
    for weekday in range(7):
        items = [r for r in rows if r["period"] == "Daily" and date.fromisoformat(r["date"]).weekday() == weekday]
        weekday_stats.append({"day": weekday, "rate": rate_of(items), "records": len(items)})
    month_groups = defaultdict(list)
    for row in rows:
        if row["period"] == "Daily":
            month_groups[row["date"][:7]].append(row)
    monthly = []
    for month, items in sorted(month_groups.items()):
        month_days = defaultdict(list)
        for item in items:
            month_days[item["date"]].append(item)
        monthly.append({"month": month, "rate": rate_of(items), "records": len(items),
                        "perfect_days": sum(all(is_complete(r) for r in group) for group in month_days.values())})
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
            pending.append({"name": name, "period": period, "streak": current, "unit": unit,
                            "quantity": row["quantity"], "goal": row["goal"], "value_unit": row["unit"]})
    with database() as conn:
        bounds = conn.execute("SELECT MIN(date) min_date,MAX(date) max_date FROM records WHERE archived=0").fetchone()
        options = conn.execute("SELECT DISTINCT name,list_name,period FROM records WHERE archived=0 ORDER BY name").fetchall()
        latest = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    week_groups = defaultdict(list)
    for row in rows:
        if row["period"] == "Daily":
            key = period_key(date.fromisoformat(row["date"]), "Weekly").isoformat()
            week_groups[key].append(row)
    week_rates = [rate_of(items) for items in week_groups.values()]
    return {
        "summary": {"done": done, "missed": total - done, "records": total,
                    "rate": round(done / total * 100, 1) if total else 0,
                    "perfect_days": sum(bool(items) and all(is_complete(r) for r in items) for items in days.values())},
        "habits": habits, "heatmap": heatmap,
        "bounds": dict(bounds) if bounds else {"min_date": None, "max_date": None},
        "options": {"habits": sorted({r["name"] for r in options}),
                    "lists": sorted({r["list_name"] for r in options if r["list_name"]}),
                    "periods": sorted({r["period"] for r in options})},
        "analytics": {"trends": {"daily": heatmap}, "weekdays": weekday_stats, "monthly": monthly,
                      "regularity": {"weekly_stddev": round(statistics.pstdev(week_rates), 1) if len(week_rates) > 1 else None,
                                     "weeks": len(week_rates)},
                      "today": {"date": today.isoformat(), "done": today_done, "total": len(current_period),
                                "pending": sorted(pending, key=lambda x: x["name"].lower())},
                      "latest_import": dict(latest) if latest else None},
    }


def habit_detail(name: str, params: dict[str, list[str]]) -> dict | None:
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
    current, longest, unit = streaks(history, date.today())
    values = [r["quantity"] for r in rows]
    return {"name": name, "period": period, "type": history[-1]["habit_type"],
            "goal": history[-1]["goal"], "unit": history[-1]["unit"], "list": history[-1]["list_name"],
            "current_streak": current, "longest_streak": longest, "streak_unit": unit,
            "done": sum(is_complete(r) for r in rows), "missed": sum(not is_complete(r) for r in rows),
            "rate": rate_of(rows) or 0, "average": round(statistics.fmean(values), 2) if values else 0,
            "minimum": min(values) if values else 0, "maximum": max(values) if values else 0,
            "records": [{"date": r["date"], "quantity": r["quantity"], "complete": is_complete(r),
                         "status": r["status"]} for r in rows]}


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

    def read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ImportError("Nieprawidłowy Content-Length") from exc
        if length <= 0:
            raise ImportError("Brak pliku w żądaniu")
        if length > MAX_UPLOAD_BYTES + 65536:
            raise ImportError(f"Żądanie przekracza limit {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                return self.json_response({"ok": True})
            if parsed.path == "/api/config":
                with database() as conn:
                    latest = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()
                host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", f"localhost:{PORT}")
                proto = self.headers.get("X-Forwarded-Proto", "http")
                return self.json_response({"webhook_url": f"{proto}://{host}/webhook/{webhook_token()}",
                                           "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
                                           "latest_import": dict(latest) if latest else None})
            if parsed.path == "/api/dashboard":
                return self.json_response(dashboard(params))
            if parsed.path.startswith("/api/habits/"):
                detail = habit_detail(unquote(parsed.path.removeprefix("/api/habits/")), params)
                return self.json_response(detail or {"error": "Nie znaleziono nawyku"}, 200 if detail else 404)
            return self.serve_static(parsed.path)
        except (ValueError, sqlite3.Error) as exc:
            return self.json_response({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        is_ui = parsed.path == "/api/import"
        is_hook = parsed.path.startswith("/webhook/") and hmac.compare_digest(
            unquote(parsed.path.removeprefix("/webhook/")), webhook_token())
        if not is_ui and not is_hook:
            return self.json_response({"error": "Nie znaleziono endpointu"}, 404)
        try:
            body = self.read_body()
            content_type = self.headers.get("Content-Type", "")
            if content_type.startswith("multipart/form-data"):
                payload, filename = extract_multipart(body, content_type)
            else:
                payload, filename = body, self.headers.get("X-Filename", "AwesomeHabits.csv")
            result = import_csv(payload, "interfejs" if is_ui else "webhook", Path(filename).name)
            return self.json_response(result, HTTPStatus.CREATED)
        except ImportError as exc:
            return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except sqlite3.Error as exc:
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


def main() -> None:
    init_db()
    webhook_token()
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
