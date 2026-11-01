"""Real M-Team and qBittorrent HTTP protocols; never log credentials or bodies."""

from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import urljoin, urlsplit

import requests

from .config import checked_proxy_url


class IntegrationError(RuntimeError):
    def __init__(self, message, *, status=None, ambiguous=False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


def checked_url(url, *, https=False, query=False):
    try:
        if not isinstance(url, str) or any(ord(c) <= 32 for c in url):
            raise ValueError
        parts = urlsplit(url)
        local = parts.hostname == "localhost"
        try:
            local = local or ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            pass
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.fragment or (parts.query and not query)
                or (https and parts.scheme != "https" and not local)):
            raise ValueError
        parts.port
    except (ValueError, TypeError):
        raise IntegrationError("连接地址无效；仅接受无内嵌凭证的 HTTP(S) 地址，站点要求 HTTPS") from None
    return url.rstrip("/") if not query else url


def request(session, method, url, stage, emit, *, max_bytes=16 * 1024 * 1024, redirects=0, **kwargs):
    """Bound decoded response size and suppress request/body/URL secrets in errors."""
    emit("INFO", stage, "HTTP 请求开始", {"method": method})
    try:
        with session.request(method, url, timeout=(10, 30), allow_redirects=False,
                             stream=True, **kwargs) as response:
            status = response.status_code
            details = {"http_status": status}
            if status in (301, 302, 303, 307, 308) and redirects:
                location = response.headers.get("Location")
                if not location:
                    raise IntegrationError(f"{stage}：重定向缺少 Location")
                target = checked_url(urljoin(url, location), https=True, query=True)
                if urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https":
                    raise IntegrationError(f"{stage}：拒绝 HTTPS 降级重定向")
                parts = urlsplit(target)
                emit("INFO", stage, "安全跟随种子下载重定向；不携带 API 凭证", {
                    **details, "target": f"{parts.scheme}://{parts.netloc}", "remaining": redirects - 1,
                })
                return request(session, "GET", target, stage, emit, max_bytes=max_bytes,
                               redirects=redirects - 1)
            if status == 429:
                retry = response.headers.get("Retry-After", "")
                if retry.isascii() and retry.isdigit() and len(retry) <= 10:
                    retry = str(int(retry))
                elif retry:
                    try:
                        retry = parsedate_to_datetime(retry).isoformat()
                    except (ValueError, TypeError, OverflowError):
                        retry = "无效的 Retry-After"
                else:
                    retry = "未提供"
                details["retry_after"] = retry
                emit("WARNING", stage, "HTTP 429 站点限流；本项未获接受，不自动重发", details)
                raise IntegrationError(f"{stage}：HTTP 429 限流，Retry-After={retry}", status=status)
            if not 200 <= status < 300:
                reason = {401: "认证失败", 403: "权限拒绝或风控", 409: "冲突或拒绝",
                          415: "种子格式被拒绝"}.get(status, "非成功 HTTP 响应")
                emit("ERROR", stage, reason, details)
                raise IntegrationError(f"{stage}：HTTP {status} {reason}", status=status,
                                       ambiguous=not (400 <= status < 500 and status != 408))
            content = bytearray()
            for chunk in response.iter_content(65536):
                if len(content) + len(chunk) > max_bytes:
                    emit("ERROR", stage, "响应超过安全字节上限", {**details, "limit": max_bytes})
                    raise IntegrationError(f"{stage}：响应超过 {max_bytes} 字节上限", ambiguous=True)
                content.extend(chunk)
            emit("INFO", stage, "HTTP 响应成功", {**details, "bytes": len(content)})
            return status, bytes(content)
    except requests.RequestException as error:
        # Exception messages often contain token URLs, cookies, or credential headers.
        emit("ERROR", stage, "HTTP 连接或 TLS 失败", {"error_type": type(error).__name__})
        raise IntegrationError(f"{stage}：连接或 TLS 失败（{type(error).__name__}）",
                               ambiguous=True) from None


def json_body(content, stage, expected):
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise IntegrationError(f"{stage}：响应不是有效 JSON") from None
    if not isinstance(value, expected):
        raise IntegrationError(f"{stage}：JSON 结构不符")
    return value


class MTeam:
    def __init__(self, config, emit):
        self.url = checked_url(config.get("url", ""), https=True)
        if not config.get("api_key"):
            raise IntegrationError("M-Team API Key 尚未配置")
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": config["api_key"], "User-Agent": "PTFlow/1.0"})
        proxy = checked_proxy_url(config.get("proxy_url", ""))
        if proxy:
            self.session.trust_env = False
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.emit = emit

    def _post(self, endpoint, stage, **kwargs):
        _, body = request(self.session, "POST", f"{self.url}/api/torrent/{endpoint}",
                          stage, self.emit, **kwargs)
        value = json_body(body, stage, dict)
        code = value.get("code")
        self.emit("INFO", stage, "站点业务响应", {"code": code, "message": value.get("message", "")})
        if code not in (0, "0", "SUCCESS"):
            raise IntegrationError(f"{stage}：站点业务失败或缺少 code")
        return value.get("data")

    def search(self, mode, page_size):
        data = self._post("search", "mteam.search", json={
            "categories": [], "mode": mode, "visible": 1, "pageNumber": 1,
            "pageSize": page_size, "sortDirection": "DESC",
            "sortField": "CREATED_DATE", "discount": "FREE",
        })
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise IntegrationError("mteam.search：data.data 应为种子列表")
        return data["data"]

    def torrent(self, identity):
        link = self._post("genDlToken", "mteam.token", data={"id": str(identity)})
        if not isinstance(link, str) or not link:
            raise IntegrationError("mteam.token：未返回下载链接")
        checked_url(link, https=True, query=True)
        if urlsplit(self.url).scheme == "https" and urlsplit(link).scheme != "https":
            raise IntegrationError("mteam.torrent：拒绝 HTTPS 站点返回明文下载凭证链接")
        # The generated download host receives its URL token, never our API key/cookies.
        with requests.Session() as download:
            download.trust_env = self.session.trust_env
            download.proxies.update(self.session.proxies)
            _, content = request(download, "GET", link, "mteam.torrent", self.emit, redirects=5)
        return content

    def close(self):
        self.session.close()


class QBittorrent:
    def __init__(self, config, emit):
        self.url = checked_url(config.get("url", ""))
        self.emit = emit
        self.session = requests.Session()
        self.session.headers["Referer"] = self.url + "/"
        auth = config.get("auth_type", "password")
        if auth == "api_key":
            if not config.get("api_key"):
                raise IntegrationError("qBittorrent API Key 尚未配置（需 ≥5.2）")
            # https://github.com/qbittorrent/qBittorrent/wiki/API-Key-Authentication-(%E2%89%A5v5.2.0)
            self.session.headers["Authorization"] = "Bearer " + config["api_key"]
        elif auth == "password":
            if not config.get("username") or not config.get("password"):
                raise IntegrationError("qBittorrent 用户名或密码尚未配置")
            status, body = self.call("auth/login", "qb.login", "POST", data={
                "username": config["username"], "password": config["password"],
            })
            # Modern WebAPI uses 204; authenticated version/stats/list reads still verify access.
            if not (status == 204 or status == 200 and body.strip() == b"Ok."):
                raise IntegrationError("qb.login：用户名或密码被拒绝")
        else:
            raise IntegrationError("不支持的 qBittorrent 认证方式")
        _, body = self.call("app/version", "qb.version")
        self.version = body.decode("utf-8", errors="replace").strip()
        match = re.fullmatch(r"v?(\d{1,3})\.(\d{1,3})(?:\.\d{1,3})?(?:alpha\d*|beta\d*|rc\d*|dev)?", self.version)
        if not match:
            raise IntegrationError("qb.version：无效的 qBittorrent 版本响应")
        self.modern = int(match[1]) >= 5
        if auth == "api_key" and (int(match[1]), int(match[2])) < (5, 2):
            raise IntegrationError("qBittorrent API Key 认证要求版本 ≥5.2")

    def call(self, endpoint, stage, method="GET", **kwargs):
        return request(self.session, method, f"{self.url}/api/v2/{endpoint}", stage, self.emit, **kwargs)

    def data(self, endpoint, stage, expected, **params):
        _, body = self.call(endpoint, stage, params=params)
        return json_body(body, stage, expected)

    def torrents(self):
        rows = self.data("torrents/info", "qb.torrents", list)
        if any(not isinstance(row, dict) or not re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", str(row.get("hash", ""))) for row in rows):
            raise IntegrationError("qb.torrents：种子记录或哈希无效")
        for row in rows:
            if not isinstance(row.get("state"), str) or not isinstance(row.get("category"), str):
                raise IntegrationError("qb.torrents：缺少状态或分类，不能判断所有权和活动")
            for key in ("size", "progress", "upspeed", "dlspeed", "uploaded", "downloaded", "ratio", "added_on"):
                value = row.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise IntegrationError(f"qb.torrents：缺少或无效的字段 {key}，不猜测空闲状态")
            if row["progress"] > 1:
                raise IntegrationError("qb.torrents：下载进度不在 0–1 范围内")
        return rows

    def sample(self):
        data = self.data("sync/maindata", "qb.stats", dict, rid=0)
        server = data.get("server_state")
        fields = {"upspeed": "up_info_speed", "dlspeed": "dl_info_speed", "uploaded": "alltime_ul",
                  "downloaded": "alltime_dl", "free_space": "free_space_on_disk"}
        if not isinstance(server, dict):
            raise IntegrationError("qb.stats：缺少全局 server_state")
        result = {}
        for key, source in fields.items():
            value = server.get(source)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise IntegrationError(f"qb.stats：缺少或无效的全局字段 {source}")
            result[key] = int(value)
        return result

    def category(self):
        categories = self.data("torrents/categories", "qb.category", dict)
        if "ptflow" not in categories:
            self.call("torrents/createCategory", "qb.category", "POST", data={"category": "ptflow", "savePath": ""})

    def add(self, content, identity, save_path):
        data = {"category": "ptflow", "tags": f"ptflow-id-{identity}", "autoTMM": "false",
                "stopped" if self.modern else "paused": "true"}
        if save_path:
            data["savepath"] = save_path
        status, body = self.call("torrents/add", "qb.add", "POST", data=data,
                                 files={"torrents": ("ptflow.torrent", content, "application/x-bittorrent")})
        accepted = add_outcome(status, body)
        self.emit("INFO" if accepted == "accepted" else "WARNING", "qb.add", "添加接口结果；仍须核验实际哈希",
                  {"http_status": status, "outcome": accepted})
        return accepted

    def files(self, torrent_hash):
        rows = self.data("torrents/files", "qb.files", list, hash=torrent_hash)
        validate_files(rows)
        for row in rows:
            value = row.get("progress")
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise IntegrationError("qb.files：缺少或无效的文件进度，不能安全判断 Free 保护")
        return rows

    def priority(self, torrent_hash, indexes, priority):
        if indexes:
            self.call("torrents/filePrio", "qb.files.priority", "POST", data={
                "hash": torrent_hash, "id": "|".join(map(str, indexes)), "priority": priority,
            })

    def stop(self, torrent_hash):
        self.call("torrents/stop" if self.modern else "torrents/pause", "qb.stop", "POST", data={"hashes": torrent_hash})

    def start(self, torrent_hash):
        self.call("torrents/start" if self.modern else "torrents/resume", "qb.start", "POST", data={"hashes": torrent_hash})

    def delete(self, torrent_hash, delete_files):
        self.call("torrents/delete", "qb.delete", "POST", data={
            "hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false",
        })

    def close(self):
        self.session.close()


def add_outcome(status, body):
    if body.strip() == b"Ok." and status == 200:
        return "accepted"
    if body.strip() == b"Fails.":
        return "rejected"
    try:
        value = json.loads(body)
        counts = [value[key] for key in ("success_count", "pending_count", "failure_count")]
        if any(type(n) is not int or n < 0 for n in counts) or sum(counts) != 1:
            return "uncertain"
        success, pending, failure = counts
        if failure == 1:
            return "rejected"
        if success == 1 and status == 200:
            return "accepted"
    except (ValueError, TypeError, KeyError, UnicodeError):
        pass
    return "uncertain"


def validate_files(files):
    indexes = set()
    if not files:
        raise IntegrationError("qb.files：文件列表为空，保持停止")
    for row in files:
        if (not isinstance(row, dict) or type(row.get("index")) is not int
                or row["index"] < 0 or row["index"] in indexes
                or type(row.get("size")) is not int or row["size"] < 0
                or type(row.get("priority")) is not int or row["priority"] not in (0, 1, 6, 7)):
            raise IntegrationError("qb.files：文件大小、索引或优先级无效，保持停止")
        indexes.add(row["index"])


def select_files(files, budget):
    """Select whole files only; smallest first makes at least one fit when possible."""
    validate_files(files)
    selected, total = [], 0
    for row in sorted(files, key=lambda row: (row["size"], row["index"])):
        if row["size"] > 0 and total + row["size"] <= budget:
            selected.append(row["index"])
            total += row["size"]
    return selected, total


def torrent_metadata(content):
    """Validate BEP 3 and hash the original info slice, never a re-encoding."""
    if not content or len(content) > 16 * 1024 * 1024:
        raise IntegrationError("torrent 校验：文件为空或超过 16MiB")
    position, info_slice = 0, None

    def parse(depth=0):
        nonlocal position, info_slice
        if depth > 64 or position >= len(content):
            raise ValueError
        first = content[position:position + 1]
        if first == b"i":
            end = content.index(b"e", position + 1)
            raw = content[position + 1:end]
            if not re.fullmatch(rb"0|-?[1-9][0-9]*", raw) or len(raw) > 20:
                raise ValueError
            position = end + 1
            return int(raw)
        if first in (b"d", b"l"):
            position += 1
            value = {} if first == b"d" else []
            while position < len(content) and content[position:position + 1] != b"e":
                if first == b"d":
                    key = parse(depth + 1)
                    if not isinstance(key, bytes) or key in value:
                        raise ValueError
                    start = position
                    value[key] = parse(depth + 1)
                    if depth == 0 and key == b"info":
                        info_slice = content[start:position]
                else:
                    value.append(parse(depth + 1))
            if position >= len(content):
                raise ValueError
            position += 1
            return value
        end = content.index(b":", position)
        raw = content[position:end]
        if not re.fullmatch(rb"0|[1-9][0-9]*", raw) or len(raw) > 10:
            raise ValueError
        length = int(raw)
        position = end + 1
        if position + length > len(content):
            raise ValueError
        value = content[position:position + length]
        position += length
        return value

    def valid_component(value):
        return (isinstance(value, bytes) and value not in (b"", b".", b"..")
                and not any(c in value for c in (b"/", b"\\", b"\x00")))

    try:
        root = parse()
        if position != len(content) or not isinstance(root, dict) or info_slice is None:
            raise ValueError
        info = root[b"info"]
        if not isinstance(info, dict) or not valid_component(info.get(b"name")):
            raise ValueError
        piece_length = info.get(b"piece length")
        pieces = info.get(b"pieces")
        if type(piece_length) is not int or piece_length <= 0 or not isinstance(pieces, bytes):
            raise ValueError
        lengths = []
        if b"files" in info:
            if b"length" in info or not isinstance(info[b"files"], list) or not info[b"files"]:
                raise ValueError
            paths = set()
            for file in info[b"files"]:
                if not isinstance(file, dict):
                    raise ValueError
                path = file.get(b"path")
                if not isinstance(path, list) or not path or not all(valid_component(p) for p in path):
                    raise ValueError
                path = tuple(path)
                if path in paths:
                    raise ValueError
                paths.add(path)
                lengths.append(file.get(b"length"))
        else:
            lengths = [info.get(b"length")]
        if any(type(n) is not int or n < 0 for n in lengths) or sum(lengths) <= 0:
            raise ValueError
        total = sum(lengths)
        if len(pieces) != ((total + piece_length - 1) // piece_length) * 20:
            raise ValueError
        return {"hash": hashlib.sha1(info_slice).hexdigest(), "size": total, "lengths": lengths}
    except (ValueError, KeyError, IndexError, RecursionError, TypeError):
        raise IntegrationError("torrent 校验：不是有效的 BEP 3 种子，或文件路径/分片结构无效（纯 v2 种子不支持）") from None
