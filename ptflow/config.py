"""Validated settings and atomic, private persistence."""
from copy import deepcopy
from datetime import datetime, time
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import math
import os
import secrets
import tempfile

DEFAULTS = {
    "downloader": {"url": "", "auth_type": "password", "username": "", "password": "", "api_key": "", "save_path": ""},
    "mteam": {"url": "https://api.m-team.cc", "api_key": "", "modes": ["movie", "tvshow", "normal"], "request_interval_seconds": 60, "page_size": 100},
    "schedule": {"enabled": False, "query_minutes": 30, "brush_minutes": 15, "cleanup_minutes": 10, "stats_seconds": 30, "work_start": "00:00", "work_end": "00:00", "timezone": "Asia/Shanghai"},
    "strategy": {"min_free_gib": 50, "max_downloading": 4, "target_upload_mib": 2, "max_download_mib": 20, "speed_window_minutes": 10, "min_free_hours": 3, "max_torrent_gib": 10, "min_seeders": 1, "min_leechers": 1, "cleanup_enabled": False, "idle_minutes": 60, "protect_seed_hours": 24, "delete_files": False},
}
SECRET_FIELDS = ("downloader.password", "downloader.api_key", "mteam.api_key")


def validate_config(value, previous=None):
    if not isinstance(value, dict):
        raise ValueError("配置必须是 JSON 对象")
    result = deepcopy(previous or DEFAULTS)
    for section, fields in value.items():
        if section == "secret_fields":
            continue
        if section not in DEFAULTS or not isinstance(fields, dict):
            raise ValueError(f"未知配置分组：{section}")
        for key, item in fields.items():
            if key not in DEFAULTS[section]:
                raise ValueError(f"未知配置：{section}.{key}")
            if f"{section}.{key}" in SECRET_FIELDS and item == "":
                continue
            result[section][key] = item
    for section, fields in DEFAULTS.items():
        for key, default in fields.items():
            item = result[section][key]
            if isinstance(default, bool):
                if not isinstance(item, bool):
                    raise ValueError(f"{section}.{key} 必须是开关值")
            elif isinstance(default, str):
                if not isinstance(item, str) or len(item) > 2048 or any(ord(c) < 32 for c in item):
                    raise ValueError(f"{section}.{key} 必须是无控制字符的文本，最多 2048 字符")
            elif isinstance(default, (int, float)):
                if isinstance(item, bool) or not isinstance(item, (int, float)) or (isinstance(item, float) and not math.isfinite(item)):
                    raise ValueError(f"{section}.{key} 必须是有效数字")
    integer_ranges = {
        "mteam.request_interval_seconds": (1, 600), "mteam.page_size": (1, 200),
        "schedule.query_minutes": (1, 10080), "schedule.brush_minutes": (1, 10080),
        "schedule.cleanup_minutes": (1, 10080), "schedule.stats_seconds": (5, 3600),
        "strategy.max_downloading": (1, 100), "strategy.speed_window_minutes": (1, 1440),
        "strategy.min_seeders": (0, 1000000), "strategy.min_leechers": (0, 1000000),
        "strategy.idle_minutes": (5, 10080),
    }
    for path, (minimum, maximum) in integer_ranges.items():
        section, key = path.split(".")
        item = result[section][key]
        if not isinstance(item, int) or not minimum <= item <= maximum:
            raise ValueError(f"{path} 必须为 {minimum}–{maximum} 的整数")
    for key in ("min_free_gib", "target_upload_mib", "max_download_mib", "min_free_hours", "max_torrent_gib", "protect_seed_hours"):
        item = result["strategy"][key]
        minimum = 0.01 if key in ("max_download_mib", "max_torrent_gib") else 0
        if not minimum <= item <= 100000:
            raise ValueError(f"strategy.{key} 必须在 {minimum}–100000 之间")
    if result["downloader"]["auth_type"] not in ("password", "api_key"):
        raise ValueError("qBittorrent 认证方式应为 password 或 api_key")
    modes = result["mteam"]["modes"]
    if not isinstance(modes, list) or not modes or any(mode not in ("movie", "adult", "tvshow", "normal", "rankings") for mode in modes) or len(set(modes)) != len(modes):
        raise ValueError("请选择至少一个有效、无重复的 M-Team 查询分类")
    for section in ("downloader", "mteam"):
        url = result[section]["url"].strip().rstrip("/")
        result[section]["url"] = url
        if not url and section == "downloader":
            continue
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError:
            raise ValueError(f"{section} 地址格式错误") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{section} 地址须为不含凭证、查询参数的 HTTP(S) 地址")
        if section == "mteam" and parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("M-Team 必须使用 HTTPS 保护 API Key")
    schedule = result["schedule"]
    for key in ("work_start", "work_end"):
        try:
            parsed_time = time.fromisoformat(schedule[key])
        except ValueError:
            raise ValueError("工作时段必须使用 HH:MM 格式") from None
        if len(schedule[key]) != 5 or parsed_time.tzinfo:
            raise ValueError("工作时段必须使用 HH:MM 格式")
    try:
        ZoneInfo(schedule["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("时区无效，例如 Asia/Shanghai") from None
    if schedule["enabled"] and (not result["downloader"]["url"] or not result["mteam"]["api_key"]):
        raise ValueError("开启自动调度前，请先填写 qBittorrent 地址和 M-Team API Key")
    return result


def is_work_time(config, now=None):
    schedule = config["schedule"]
    current = (now or datetime.now(ZoneInfo(schedule["timezone"]))).astimezone(ZoneInfo(schedule["timezone"])).time().replace(tzinfo=None)
    start, end = time.fromisoformat(schedule["work_start"]), time.fromisoformat(schedule["work_end"])
    if start == end:
        return True
    return start <= current < end if start < end else current >= start or current < end


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Config:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "config.json"
        self.auth_path = Path(data_dir) / "auth.json"
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.value = validate_config(json.loads(self.path.read_text()) if self.path.exists() else {})
        if not self.path.exists():
            atomic_json(self.path, self.value)
        self.auth = json.loads(self.auth_path.read_text()) if self.auth_path.exists() else {"secret_key": secrets.token_hex(32), "password_hash": "", "generation": 0}
        if not self.auth_path.exists():
            atomic_json(self.auth_path, self.auth)
        os.chmod(self.path, 0o600)
        os.chmod(self.auth_path, 0o600)

    def get(self):
        with self.lock:
            return deepcopy(self.value)

    def public(self):
        result = self.get()
        result["secret_fields"] = []
        for path in SECRET_FIELDS:
            section, key = path.split(".")
            if result[section][key]:
                result["secret_fields"].append(path)
            result[section][key] = ""
        return result

    def save(self, value):
        with self.lock:
            new = validate_config(value, self.value)
            atomic_json(self.path, new)
            self.value = new
        return self.public()

    def secrets(self):
        value = self.get()
        return [value[section][key] for section, key in (path.split(".") for path in SECRET_FIELDS)]

    def set_password_hash(self, password_hash):
        with self.lock:
            new = {**self.auth, "password_hash": password_hash, "generation": self.auth["generation"] + 1}
            atomic_json(self.auth_path, new)
            self.auth = new
