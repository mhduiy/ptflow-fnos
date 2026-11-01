"""SQLite state, bounded history and credential-safe structured logs."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import local
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import re
import sqlite3
import time


def account_calendar(zone, now):
    current = datetime.fromtimestamp(now, ZoneInfo(zone))
    midnight = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), current.tzinfo)
    return current.date().isoformat(), midnight.timestamp()


def account_source(settings):
    parts = urlsplit(settings["url"])
    origin = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    fingerprint = hashlib.sha256(json.dumps([origin, settings["api_key"]]).encode()).hexdigest()
    return origin, fingerprint


class Store:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "ptflow.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets = []
        self.context = local()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                    seeders INTEGER NOT NULL, leechers INTEGER NOT NULL, free_until REAL NOT NULL,
                    score REAL NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                    qb_hash TEXT NOT NULL DEFAULT '', last_activity REAL NOT NULL DEFAULT 0,
                    submitted_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                    level TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '', details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    time REAL PRIMARY KEY, upspeed INTEGER NOT NULL, dlspeed INTEGER NOT NULL,
                    uploaded INTEGER NOT NULL, downloaded INTEGER NOT NULL, free_space INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL NOT NULL, status TEXT NOT NULL,
                    message TEXT NOT NULL, counts TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_sources (
                    fingerprint TEXT PRIMARY KEY, origin TEXT NOT NULL, account_id TEXT,
                    last_attempt_at REAL NOT NULL, next_at REAL NOT NULL,
                    timezone TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    origin TEXT NOT NULL, account_id TEXT NOT NULL, day TEXT NOT NULL,
                    time REAL NOT NULL, bonus REAL NOT NULL, uploaded INTEGER NOT NULL,
                    downloaded INTEGER NOT NULL, ratio REAL,
                    PRIMARY KEY(origin,account_id,day)
                );
                CREATE INDEX IF NOT EXISTS event_run ON events(run_id,id);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def redact(self, value):
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.redact(item) for item in value]
        if not isinstance(value, str):
            return value
        text = value
        for secret in sorted((item for item in self.secrets if item), key=len, reverse=True):
            text = text.replace(secret, "***")
        text = re.sub(r"(https?://)[^/\s]+@", r"\1***@", text)
        text = re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*", r"\1?***", text)
        text = re.sub(r"(?i)((?:api[_-]?key|token|password|passkey|cookie|authorization)\s*[=:]\s*)[^\s,;]+", r"\1***", text)
        return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)[:12000]

    @contextmanager
    def run_context(self, run_id):
        previous = getattr(self.context, "run_id", "")
        self.context.run_id = run_id
        try:
            yield
        finally:
            self.context.run_id = previous

    def event(self, level, stage, message, run_id=None, details=None):
        row = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level.upper(), "stage": self.redact(stage), "message": self.redact(str(message)),
            "run_id": run_id or getattr(self.context, "run_id", ""),
            "details": self.redact(details or {}),
        }
        with self.connection() as db:
            cursor = db.execute("INSERT INTO events(time,level,stage,message,run_id,details) VALUES(?,?,?,?,?,?)", (row["time"], row["level"], row["stage"], row["message"], row["run_id"], json.dumps(row["details"], ensure_ascii=False)))
            identifier = cursor.lastrowid
            if identifier % 100 == 0:
                db.execute("DELETE FROM events WHERE id <= ?", (identifier - 10000,))
        if not os.environ.get("SOCKET_PATH"):
            print(json.dumps({"id": identifier, **row}, ensure_ascii=False), flush=True)
        return identifier

    def event_bounds(self):
        with self.connection() as db:
            row = db.execute("SELECT COALESCE(MIN(id),0),COALESCE(MAX(id),0) FROM events").fetchone()
            return tuple(row)

    def events(self, after=0, limit=200, level="", stage="", q="", run_id=""):
        clauses, values = ["id > ?"], [max(0, after)]
        for key, value in (("level", level), ("stage", stage), ("run_id", run_id)):
            if value:
                clauses.append(f"{key} = ?")
                values.append(value)
        if q:
            clauses.append("(instr(lower(message),lower(?))>0 OR instr(lower(details),lower(?))>0)")
            values.extend((q, q))
        order = "ASC" if after else "DESC"
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id {order} LIMIT ?", (*values, min(max(1, limit), 10099))).fetchall()
        result = [dict(row) for row in rows]
        if not after:
            result.reverse()
        for row in result:
            row["details"] = json.loads(row["details"])
        return result

    def upsert_candidate(self, candidate):
        keys = ("id", "name", "size", "seeders", "leechers", "free_until", "score", "mode")
        values = [candidate[key] for key in keys]
        values[0] = str(values[0])
        with self.connection() as db:
            db.execute("""INSERT INTO candidates(id,name,size,seeders,leechers,free_until,score,mode,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,size=excluded.size,seeders=excluded.seeders,
                leechers=excluded.leechers,free_until=excluded.free_until,score=excluded.score,mode=excluded.mode,updated_at=excluded.updated_at""", (*values, time.time()))

    def candidates(self, limit=500, *, minimum_size=None, maximum_size=None):
        clauses, values = [], []
        if minimum_size is not None:
            clauses.append("size >= ?")
            values.append(minimum_size)
        if maximum_size is not None:
            clauses.append("size <= ?")
            values.append(maximum_size)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM candidates{where} ORDER BY score DESC,id LIMIT ?", (*values, min(max(1, limit), 100000)))]

    def prune_candidates(self, minimum_size, maximum_size):
        with self.connection() as db:
            return db.execute("""DELETE FROM candidates WHERE state IN ('pending','failed') AND qb_hash = ''
                AND (size < ? OR size > ?)""", (minimum_size, maximum_size)).rowcount

    def update_candidate(self, identifier, **fields):
        allowed = {"state", "reason", "qb_hash", "last_activity", "submitted_at", "size"}
        if not fields or set(fields) - allowed:
            raise ValueError("无效的候选更新字段")
        with self.connection() as db:
            db.execute(f"UPDATE candidates SET {','.join(key+'=?' for key in fields)} WHERE id=?", (*fields.values(), str(identifier)))

    def summary(self):
        result = {"pending": 0, "submitted": 0, "failed": 0, "uncertain": 0}
        with self.connection() as db:
            for row in db.execute("SELECT state,COUNT(*) AS n FROM candidates GROUP BY state"):
                result[row["state"]] = row["n"]
        return result

    def sample(self, value):
        keys = ("time", "upspeed", "dlspeed", "uploaded", "downloaded", "free_space")
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?)", tuple(value[key] for key in keys))
            db.execute("DELETE FROM samples WHERE time < ?", (time.time() - 7 * 86400,))

    def latest_sample(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM samples ORDER BY time DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def speed_window(self, minutes):
        now = time.time()
        with self.connection() as db:
            return dict(db.execute("""SELECT COUNT(*) AS samples, MAX(time) AS time,
                MAX(dlspeed) AS peak_download, AVG(upspeed) AS average_upload
                FROM samples WHERE time BETWEEN ? AND ?""", (now - minutes * 60, now)).fetchone())

    def history(self, minutes=60):
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM samples WHERE time>=? ORDER BY time", (time.time() - min(max(minutes, 1), 10080) * 60,))]
        if len(rows) > 1000:
            step = (len(rows) + 998) // 999
            return rows[:-1:step] + rows[-1:]
        return rows

    def account_stats(self, settings, zone, days=90, now=None):
        now = time.time() if now is None else now
        configured = bool(settings["url"] and settings["api_key"])
        result = {"configured": configured, "latest": None, "history": [], "error": "",
                  "last_attempt_at": None, "next_at": now if configured else None}
        if not configured:
            return result
        origin, fingerprint = account_source(settings)
        day, midnight = account_calendar(zone, now)
        since = (datetime.fromisoformat(day).date() - timedelta(days=min(max(days, 1), 3650) - 1)).isoformat()
        with self.connection() as db:
            source = db.execute("SELECT * FROM account_sources WHERE fingerprint=?", (fingerprint,)).fetchone()
            if source is None:
                return result
            result.update(error=source["error"], last_attempt_at=source["last_attempt_at"])
            if source["timezone"] == zone:
                result["next_at"] = max(now, source["next_at"])
            elif account_calendar(zone, source["last_attempt_at"])[0] >= day:
                result["next_at"] = midnight
            if source["account_id"] is not None:
                fields = "day,time,bonus,uploaded,downloaded,ratio"
                values = (origin, source["account_id"])
                latest = db.execute(f"SELECT {fields} FROM account_snapshots WHERE origin=? AND account_id=? ORDER BY day DESC LIMIT 1", values).fetchone()
                result["latest"] = dict(latest) if latest else None
                result["history"] = [dict(row) for row in db.execute(f"SELECT {fields} FROM account_snapshots WHERE origin=? AND account_id=? AND day>=? ORDER BY day", (*values, since))]
        return result

    def begin_account_sample(self, settings, zone, now):
        if not settings["url"] or not settings["api_key"]:
            return False
        origin, fingerprint = account_source(settings)
        day, midnight = account_calendar(zone, now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute("SELECT * FROM account_sources WHERE fingerprint=?", (fingerprint,)).fetchone()
            if source is not None:
                if source["timezone"] == zone and source["next_at"] > now:
                    return False
                if source["timezone"] != zone and account_calendar(zone, source["last_attempt_at"])[0] >= day:
                    return False
            db.execute("""INSERT INTO account_sources(fingerprint,origin,last_attempt_at,next_at,timezone,error)
                VALUES(?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,next_at=excluded.next_at,timezone=excluded.timezone,error=excluded.error""",
                (fingerprint, origin, now, midnight, zone, "统计采样已开始；若进程中断，将保留上次成功数据并于下次午夜再采样"))
        return True

    def finish_account_sample(self, settings, zone, attempted_at, profile=None, error=""):
        origin, fingerprint = account_source(settings)
        day, _ = account_calendar(zone, attempted_at)
        with self.connection() as db:
            if profile is not None:
                keys = ("bonus", "uploaded", "downloaded", "ratio")
                db.execute("""INSERT INTO account_snapshots VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(origin,account_id,day) DO UPDATE SET time=excluded.time,
                    bonus=excluded.bonus,uploaded=excluded.uploaded,downloaded=excluded.downloaded,ratio=excluded.ratio""",
                    (origin, profile["account_id"], day, attempted_at, *[profile[key] for key in keys]))
                db.execute("UPDATE account_sources SET account_id=?,error='' WHERE fingerprint=?",
                           (profile["account_id"], fingerprint))
            else:
                db.execute("UPDATE account_sources SET error=? WHERE fingerprint=?", (self.redact(error), fingerprint))

    def finish_run(self, run):
        if run["kind"] in ("collect", "account") and run["status"] == "succeeded":
            return  # routine samples already live in history; don't bury user-triggered runs
        cleaned = self.redact(run)
        keys = ("id", "kind", "source", "started_at", "finished_at", "status", "message")
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?)", (*[cleaned[key] for key in keys], json.dumps(cleaned["counts"], ensure_ascii=False)))
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY started_at DESC LIMIT 200)")

    def runs(self, limit=30):
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (min(max(limit, 1), 200),))]
        for row in rows:
            row["counts"] = json.loads(row["counts"])
        return rows
