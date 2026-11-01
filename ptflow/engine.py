"""Persisted ownership and conservative, restart-safe brush decisions."""

from datetime import datetime
import math
import logging
import time
from threading import Event, Thread
from zoneinfo import ZoneInfo

from .config import torrent_size_bounds
from .integrations import IntegrationError, MTeam, QBittorrent, torrent_metadata

GIB = 1024 ** 3
MIB = 1024 ** 2
STOPPED = {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}


def result(status, message, **counts):
    return {"status": status, "message": message, "counts": counts}


def candidate(item, mode, now):
    if not isinstance(item, dict) or not isinstance(item.get("status"), dict):
        raise ValueError("种子项或 status 不是对象")
    status = item["status"]
    mall = status.get("mallSingleFree")
    mall = mall if isinstance(mall, dict) else {}
    if status.get("discount") in ("FREE", "_2X_FREE"):
        expiry = status.get("discountEndTime") or (mall.get("endDate") if mall.get("status") == "ONGOING" else None)
    elif mall.get("status") == "ONGOING":
        expiry = mall.get("endDate")
    else:
        raise ValueError("不满足 Free 规则")
    if not isinstance(expiry, str) or not expiry:
        raise ValueError("Free 结束时间缺失")
    try:
        end = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        free_until = end.timestamp()
    except (ValueError, OverflowError):
        raise ValueError("Free 结束时间无法解析") from None
    if free_until <= now:
        raise ValueError("Free 已结束")
    identity = str(item.get("id", ""))
    name = item.get("name")
    if not identity.isascii() or not identity.isdigit() or len(identity) > 30 or not isinstance(name, str) or not name.strip():
        raise ValueError("种子 ID 或名称无效")
    try:
        values = (item["size"], status["seeders"], status["leechers"])
        if any(type(v) is not int and not (isinstance(v, str) and v.isascii() and v.isdigit() and len(v) <= 19) for v in values):
            raise ValueError
        size, seeders, leechers = int(item["size"]), int(status["seeders"]), int(status["leechers"])
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ValueError("种子大小或人数无效") from None
    if not 0 < size <= 2**63 - 1 or not 0 <= seeders <= 2**31 - 1 or not 0 <= leechers <= 2**31 - 1:
        raise ValueError("种子大小或人数越界")
    score = (leechers / math.sqrt(seeders + 1) * math.log(max(1, size // MIB))
             * math.log(seeders + 1)) if seeders and leechers else 0
    return {"id": identity, "name": name, "size": size, "seeders": seeders,
            "leechers": leechers, "free_until": free_until, "score": score,
            "mode": mode, "state": "pending", "reason": ""}


def owned(rows, candidates):
    confirmed = {str(c["qb_hash"]).lower(): c for c in candidates
                 if c.get("state") == "submitted" and c.get("qb_hash")}
    return [(row, confirmed[str(row["hash"]).lower()]) for row in rows
            if row.get("category") == "ptflow" and str(row["hash"]).lower() in confirmed
            and f"ptflow-id-{confirmed[str(row['hash']).lower()]['id']}" in
            {tag.strip() for tag in str(row.get("tags", "")).split(",")}]


def matching(rows, expected, identity):
    for row in rows:
        tags = {tag.strip() for tag in str(row.get("tags", "")).split(",")}
        if (str(row["hash"]).lower() == expected.lower() and row.get("category") == "ptflow"
                and f"ptflow-id-{identity}" in tags):
            return row
    return None


class Engine:
    def __init__(self, config, store, progress, stopping=None):
        self.config, self.store, self.progress = config, store, progress
        self.strategy = config["strategy"]
        self.stopping = stopping

    def _event(self, level, stage, message, details=None):
        safe = self.store.redact(message)
        self.progress(stage, safe)
        self.store.event(level, stage, safe, details=details)

    def _qb(self):
        return QBittorrent(self.config["downloader"], self._event)

    def _mt(self):
        return MTeam(self.config["mteam"], self._event)

    def _start_site_protection(self, counts):
        stopped, protector = Event(), None
        run_id = getattr(self.store.context, "run_id", "")

        def protect():
            # Read-only site HTTP overlaps Free maintenance; QB add/delete stays single-flight.
            engine = Engine(self.config, self.store, lambda stage, message: None)
            with self.store.run_context(run_id):
                while not stopped.wait(self.config["schedule"]["stats_seconds"]):
                    try:
                        outcome = engine.collect()
                        counts["protection_failed"] += outcome["counts"]["failed"]
                    except Exception as error:
                        counts["protection_failed"] += 1
                        message = self.store.redact(f"Free 保护检查失败：{type(error).__name__}：{error}")
                        logging.getLogger(__name__).error(message)
                        try:
                            engine._event("ERROR", "free.protect", message)
                        except Exception as log_error:
                            logging.getLogger(__name__).error(self.store.redact(f"Free 保护错误日志持久化失败：{type(log_error).__name__}：{log_error}"))

        if self.config["downloader"]["url"] and self.store.summary()["submitted"]:
            protector = Thread(target=protect, name="ptflow-site-protection", daemon=True)
            protector.start()
        return stopped, protector

    def account(self):
        settings, zone = self.config["mteam"], self.config["schedule"]["timezone"]
        attempted_at = time.time()
        counts = {"sampled": 0, "failed": 0, "protection_failed": 0}
        if not self.store.begin_account_sample(settings, zone, attempted_at):
            return result("skipped", "MT 未配置或今日已尝试采样；未重复请求站点", **counts)
        mt, stopped, protector = None, Event(), None
        try:
            stopped, protector = self._start_site_protection(counts)
            mt = self._mt()
            profile = mt.profile()
            self.store.finish_account_sample(settings, zone, attempted_at, profile=profile)
            counts["sampled"] = 1
        except Exception as error:
            counts["failed"] = 1
            message = self.store.redact(f"MT 每日统计失败：{type(error).__name__}：{error}")
            self.store.finish_account_sample(settings, zone, attempted_at, error=message)
            self._event("ERROR", "mteam.account", message)
        finally:
            stopped.set()
            if protector is not None:
                protector.join()
            if mt is not None:
                mt.close()
        if counts["failed"]:
            return result("failed", message + "；保留上次成功数据，下次午夜再采样", **counts)
        message = "MT 每日统计已保存；未新增下载器任务"
        if counts["protection_failed"]:
            message += f"；Free 保护检查失败 {counts['protection_failed']} 次，请检查下载器状态"
        return result("partial" if counts["protection_failed"] else "succeeded", message, **counts)

    def _reason(self, identity, message, *, state=None):
        safe = self.store.redact(str(message))
        self.store.update_candidate(identity, reason=safe, **({"state": state} if state else {}))
        self._event("WARNING", "candidate", safe, {"id": identity, "state": state or "unchanged"})

    def query(self):
        self._event("INFO", "query", "串行查询 Free 候选；入库不等于提交下载器")
        minimum_size, maximum_size = torrent_size_bounds(self.strategy)
        self.store.prune_candidates(minimum_size, maximum_size)
        mt = self._mt()
        settings = self.config["mteam"]
        counts = {"returned": 0, "persisted": 0, "skipped": 0, "failed": 0, "protection_failed": 0}
        stopped, protector = Event(), None

        try:
            modes = settings["modes"]
            if not modes:
                return result("skipped", "没有启用查询分类", **counts)
            stopped, protector = self._start_site_protection(counts)
            for index, mode in enumerate(modes):
                if index:
                    delay = settings["request_interval_seconds"]
                    self._event("INFO", "query.wait", "主动请求间隔；不是 HTTP 限流", {"seconds": delay, "next_mode": mode})
                    if self.stopping is None:
                        time.sleep(delay)
                    elif self.stopping.wait(delay):
                        raise InterruptedError("应用停止，查询请求间隔已中断；未继续查询站点")
                self._event("INFO", "query.mode", "开始查询分类", {"mode": mode, "page_size": settings["page_size"]})
                try:
                    rows = mt.search(mode, settings["page_size"])
                except IntegrationError as error:
                    counts["failed"] += 1
                    self._event("ERROR", "query.mode", str(error), {"mode": mode, "http_status": error.status})
                    continue
                counts["returned"] += len(rows)
                for item in rows:
                    try:
                        row = candidate(item, mode, time.time())
                        if not minimum_size <= row["size"] <= maximum_size:
                            raise ValueError("种子总大小超出最小/最大范围；不入候选列表")
                    except (ValueError, TypeError) as error:
                        counts["skipped"] += 1
                        self._event("INFO", "query.filter", str(error), {
                            "mode": mode, "id": item.get("id") if isinstance(item, dict) else None,
                        })
                        continue
                    self.store.upsert_candidate(row)
                    counts["persisted"] += 1
                    self._event("INFO", "query.persist", "Free 候选已入库；尚未提交 QB", {
                        "id": row["id"], "name": row["name"], "size": row["size"],
                        "seeders": row["seeders"], "leechers": row["leechers"],
                        "score": row["score"], "free_until": row["free_until"], "mode": mode,
                    })
            stopped.set()
            if protector is not None:
                protector.join()
            status = "partial" if counts["protection_failed"] else "partial" if counts["failed"] and counts["persisted"] else "failed" if counts["failed"] else "succeeded"
            message = f"查询完成：入库/更新 {counts['persisted']} 项，跳过 {counts['skipped']} 项，失败分类 {counts['failed']}；未提交 QB"
            if counts["protection_failed"]:
                message += f"；Free 保护检查失败 {counts['protection_failed']} 次，请检查下载器状态"
            self._event("INFO", "query", message, counts)
            return result(status, message, **counts)
        finally:
            stopped.set()
            if protector is not None:
                protector.join()
            mt.close()

    def _observe(self, rows):
        now = time.time()
        for row, record in owned(rows, self.store.candidates(limit=100000)):
            previous = float(record.get("last_activity") or record.get("submitted_at") or now)
            activity = row.get("last_activity", 0)
            activity = activity if isinstance(activity, (int, float)) and 0 < activity <= now else 0
            if row.get("upspeed", 0) > 0 or row.get("dlspeed", 0) > 0:
                activity = now
            self.store.update_candidate(record["id"], last_activity=max(previous, activity))

    def _sample(self, qb):
        sample = qb.sample()
        sample["time"] = time.time()
        self.store.sample(sample)
        self._event("INFO", "collect", "记录下载器全局真实采样（不是 PTFlow 独占流量）", sample)
        return sample

    def _expire(self, qb, rows):
        count, failed = 0, 0
        horizon = time.time() + self.strategy["min_free_hours"] * 3600
        minimum_size, maximum_size = torrent_size_bounds(self.strategy)
        for row, record in owned(rows, self.store.candidates(limit=100000)):
            if record["free_until"] > horizon or (row.get("progress", 0) >= 1 and row.get("dlspeed", 0) <= 0):
                continue
            torrent_hash = row["hash"]
            try:
                # Stop before reading files: a failed read must not leave a download running.
                running = row.get("state") not in STOPPED or row.get("dlspeed", 0) > 0
                if running:
                    qb.stop(torrent_hash)
                    stopped_row = matching(qb.torrents(), torrent_hash, record["id"])
                    if not stopped_row or stopped_row.get("state") not in STOPPED or stopped_row.get("dlspeed", 0) > 0:
                        raise IntegrationError("临期下载停止状态未确认，禁止新增")
                files = qb.files(torrent_hash)
                may_start = running or minimum_size <= sum(f["size"] for f in files) <= maximum_size
                incomplete = [f["index"] for f in files if f.get("progress", 0) < 1 and f["priority"] != 0]
                if not incomplete:
                    if running:
                        qb.start(torrent_hash)
                    continue
                qb.priority(torrent_hash, incomplete, 0)
                after = qb.files(torrent_hash)
                if any(f.get("progress", 0) < 1 and f["priority"] != 0 for f in after):
                    raise IntegrationError("临近 Free 结束：文件优先级未生效，保持停止")
                if may_start:
                    qb.start(torrent_hash)
                prefix = "待准备：" if record.get("reason", "").startswith("待准备：") else ""
                self.store.update_candidate(record["id"], reason=prefix + "Free 窗口不足：停止所有未完成文件下载，保留数据供做种")
                self._event("INFO", "free.protect", "已禁用未完成文件下载；保留种子和已下载数据做种", {
                    "id": record["id"], "hash": torrent_hash, "files_disabled": len(incomplete),
                    "free_until": record["free_until"],
                })
                count += 1
            except IntegrationError as error:
                failed += 1
                self._reason(record["id"], str(error) + "；Free 保护未确认，禁止新增；请核实 QB 停止状态")
        return count, failed

    def collect(self):
        self._event("INFO", "collect", "开始采集真实下载器状态")
        qb = self._qb()
        try:
            self._sample(qb)
            rows = qb.torrents()
            self._observe(rows)
            protected, failed = self._expire(qb, rows)
            return result("partial" if failed else "succeeded", "下载器全局采样已更新", sampled=1,
                          managed=len(owned(rows, self.store.candidates(limit=100000))), protected=protected, failed=failed)
        finally:
            qb.close()

    def torrents(self):
        qb = self._qb()
        try:
            rows = qb.torrents()
            self._observe(rows)
            items = []
            for row, record in owned(rows, self.store.candidates(limit=100000)):
                items.append({
                    "hash": row["hash"], "id": record["id"], "name": row.get("name", record["name"]),
                    "size": row.get("total_size", record["size"]), "progress": min(1, max(0, row.get("progress", 0))),
                    "state": row.get("state", "unknown"), "upspeed": row.get("upspeed", 0),
                    "dlspeed": row.get("dlspeed", 0), "uploaded": row.get("uploaded", 0),
                    "downloaded": row.get("downloaded", 0), "ratio": row.get("ratio", 0),
                    "free_until": record["free_until"], "added_on": row.get("added_on", record.get("submitted_at", 0)),
                })
            return items
        finally:
            qb.close()

    def _confirm(self, qb, record):
        row = matching(qb.torrents(), record["qb_hash"], record["id"])
        if row is None:
            self._reason(record["id"], "QB 添加结果未确认：等待实际哈希和所有权标签出现；不重复添加", state="uncertain")
            return None
        self.store.update_candidate(record["id"], state="submitted", qb_hash=row["hash"].lower(),
                                    submitted_at=record.get("submitted_at") or time.time(),
                                    last_activity=time.time(), reason="待准备：实际哈希已确认；尚未确认下载启动")
        self._event("INFO", "qb.confirm", "实际 QB 哈希和分类/标签已确认，持久化所有权", {
            "id": record["id"], "hash": row["hash"], "state": row.get("state"),
        })
        if row.get("state") not in STOPPED or row.get("dlspeed", 0) > 0:
            qb.stop(row["hash"])
            row = matching(qb.torrents(), row["hash"], record["id"])
            if row is None or row.get("state") not in STOPPED or row.get("dlspeed", 0) > 0:
                raise IntegrationError("已确认的添加任务未能停止；保留所有权，不启动未核验的文件")
        return row

    def _prepare(self, qb, row, record, disk_available):
        torrent_hash = row["hash"]
        if row.get("state") not in STOPPED or row.get("dlspeed", 0) > 0:
            qb.stop(torrent_hash)
            stopped = matching(qb.torrents(), torrent_hash, record["id"])
            if not stopped or stopped.get("state") not in STOPPED or stopped.get("dlspeed", 0) > 0:
                raise IntegrationError("QB 停止状态未确认，禁止准备或启动文件")
        files = qb.files(torrent_hash)
        size = sum(f["size"] for f in files)
        self.store.update_candidate(record["id"], size=size)
        minimum_size, maximum_size = torrent_size_bounds(self.strategy)
        if not minimum_size <= size <= maximum_size:
            self._reason(record["id"], "待准备：种子总大小超出最小/最大范围；保留所有权并保持停止")
            return 0
        if size > disk_available:
            self._reason(record["id"], "待准备：磁盘余量无法容纳整个种子；保持停止，不缩减文件")
            return 0
        # No downloading can start until all priorities and read-back succeed.
        qb.priority(torrent_hash, [f["index"] for f in files], 1)
        after = qb.files(torrent_hash)
        if ({(f["index"], f["size"]) for f in after} != {(f["index"], f["size"]) for f in files}
                or any(f["priority"] == 0 for f in after)):
            raise IntegrationError("全文件优先级校验失败：保持停止，未启动下载")
        if record["free_until"] <= time.time() + self.strategy["min_free_hours"] * 3600:
            self._reason(record["id"], "待准备：准备文件期间 Free 窗口不足；保持停止")
            return 0
        qb.start(torrent_hash)
        actual_row = matching(qb.torrents(), torrent_hash, record["id"])
        state = actual_row.get("state") if actual_row else "未确认"
        self.store.update_candidate(record["id"], reason=f"已核验种子总大小并启用全部文件；实际 QB 状态：{state}")
        self._event("INFO", "qb.start", "种子总大小和全部文件已核验；启动接口成功不等于正在下载", {
            "id": record["id"], "hash": torrent_hash, "files": len(files),
            "total_bytes": size, "disk_available": disk_available, "state": state,
        })
        return size

    def _gate(self, sample, rows):
        settings = self.strategy
        history = self.store.speed_window(minutes=settings["speed_window_minutes"])
        fresh_age = max(60, self.config["schedule"]["stats_seconds"] * 2)
        now = time.time()
        if not history["samples"] or now - history["time"] > fresh_age:
            return "缺少新鲜下载器采样，不新增", 0, 0
        reserve = int(settings["min_free_gib"] * GIB)
        budget = max(0, sample["free_space"] - reserve)
        peak, average = history["peak_download"], history["average_upload"]
        # Count global incomplete running/queued tasks, including seeds outside our ownership.
        downloading = sum(1 for row in rows if row.get("progress", 0) < 1 and row.get("state") not in STOPPED)
        slots = max(0, settings["max_downloading"] - downloading)
        self._event("INFO", "brush.limits", "本地磁盘、速度、并发保护判定；不是站点 HTTP 限流", {
            "free_space": sample["free_space"], "reserve": reserve, "budget": budget,
            "peak_download": peak, "download_limit": settings["max_download_mib"] * MIB,
            "average_upload": average, "upload_target": settings["target_upload_mib"] * MIB,
            "window_minutes": settings["speed_window_minutes"], "samples": history["samples"],
            "sample_age_seconds": now - history["time"], "downloading": downloading, "slots": slots,
        })
        if not budget:
            return "磁盘剩余不足保留阈值，不新增", slots, budget
        if peak > settings["max_download_mib"] * MIB:
            return "窗口内下载峰值超过限制，不新增", slots, budget
        if average > settings["target_upload_mib"] * MIB:
            return "窗口内平均上传达到目标，不新增", slots, budget
        if not slots:
            return "同时下载任务达到上限，不新增", slots, budget
        return None, slots, budget

    def brush(self):
        self._event("INFO", "brush", "读取真实 QB 状态，先核对不确定添加结果，再检查保护")
        qb = self._qb()
        mt = None
        counts = {"submitted": 0, "started": 0, "reconciled": 0, "uncertain": 0, "failed": 0, "skipped": 0}
        minimum_size, maximum_size = torrent_size_bounds(self.strategy)
        try:
            self.store.prune_candidates(minimum_size, maximum_size)
            sample = self._sample(qb)
            rows = qb.torrents()
            self._observe(rows)
            _, counts["failed"] = self._expire(qb, rows)
            if counts["failed"]:
                message = "Free 保护未确认：已请求停止临期下载，本轮禁止新增；检查保护错误日志"
                self._event("ERROR", "free.protect", message, counts)
                return result("failed", message, **counts)
            recovered = []
            uncertain_size = 0
            for record in self.store.candidates(limit=100000):
                if record["state"] != "uncertain":
                    continue
                if not record.get("qb_hash"):
                    self._reason(record["id"], "不确定记录缺少预期哈希：禁止重发添加请求")
                    counts["uncertain"] += 1
                    uncertain_size += record["size"]
                    continue
                confirmed = self._confirm(qb, record)
                if confirmed:
                    counts["reconciled"] += 1
                    recovered.append((confirmed, record))
                else:
                    counts["uncertain"] += 1
                    uncertain_size += record["size"]
            rows = qb.torrents()
            recovered.extend((row, record) for row, record in owned(rows, self.store.candidates(limit=100000))
                             if record.get("reason", "").startswith("待准备：")
                             and record["id"] not in {r["id"] for _, r in recovered})
            for row, record in recovered:
                if row.get("state") not in STOPPED or row.get("dlspeed", 0) > 0:
                    qb.stop(row["hash"])
            rows = qb.torrents()
            reason, slots, disk_budget = self._gate(sample, rows)
            if reason:
                self._event("WARNING", "brush.limits", reason)
                return result("partial" if counts["uncertain"] else "skipped", reason, **counts)
            # Reserve absent ambiguous acceptances; late QB visibility must not overfill slots/disk.
            slots = max(0, slots - counts["uncertain"])
            disk_budget = max(0, disk_budget - uncertain_size)
            for row, record in recovered:
                if not slots:
                    break
                if record["free_until"] <= time.time() + self.strategy["min_free_hours"] * 3600:
                    _, failed = self._expire(qb, [row])
                    counts["failed"] += failed
                    if failed:
                        return result("partial" if counts["reconciled"] else "failed", "Free 保护未确认，本轮禁止新增", **counts)
                    continue
                try:
                    size = self._prepare(qb, row, record, disk_budget)
                    if size:
                        slots -= 1
                        disk_budget -= size
                        counts["started"] += 1
                except IntegrationError as error:
                    counts["failed"] += 1
                    self._reason(record["id"], "待准备：" + str(error))
            pending = [c for c in self.store.candidates(limit=100000, minimum_size=minimum_size,
                                                      maximum_size=maximum_size)
                       if c["state"] in ("pending", "failed") and not c.get("qb_hash")]
            pending.sort(key=lambda c: (-c["score"], c["id"]))
            for record in pending:
                if not slots or disk_budget <= 0:
                    break
                identity = record["id"]
                if record["size"] > disk_budget:
                    counts["skipped"] += 1
                    self._reason(identity, "磁盘余量无法容纳整个种子；未下载种子，不缩减文件")
                    continue
                if record["free_until"] <= time.time() + self.strategy["min_free_hours"] * 3600:
                    counts["skipped"] += 1
                    self._reason(identity, "Free 剩余时间不足最低下载窗口")
                    continue
                if record["seeders"] < self.strategy["min_seeders"] or record["leechers"] < self.strategy["min_leechers"]:
                    counts["skipped"] += 1
                    self._reason(identity, "做种数或下载数低于策略要求")
                    continue
                attempted = False
                add_returned = False
                try:
                    if mt is None:
                        mt = self._mt()
                    self._event("INFO", "brush.candidate", "按评分处理候选：先取凭证和种子，再向 QB 添加", {
                        "id": identity, "score": record["score"], "size": record["size"],
                    })
                    content = mt.torrent(identity)
                    metadata = torrent_metadata(content)
                    size = metadata["size"]
                    self.store.update_candidate(identity, size=size)
                    record = {**record, "size": size}
                    if not minimum_size <= size <= maximum_size:
                        counts["skipped"] += 1
                        self._reason(identity, "实际种子总大小超出最小/最大范围；未向 QB 添加")
                        self.store.prune_candidates(minimum_size, maximum_size)
                        continue
                    if size > disk_budget:
                        counts["skipped"] += 1
                        self._reason(identity, "磁盘余量无法容纳实际整个种子；未向 QB 添加，不缩减文件")
                        continue
                    expected = metadata["hash"]
                    rows = qb.torrents()
                    persisted = self.store.candidates(limit=100000)
                    if (any(str(row["hash"]).lower() == expected for row in rows)
                            or any(c.get("qb_hash") == expected and c["state"] in ("submitted", "uncertain") for c in persisted)):
                        counts["skipped"] += 1
                        self._reason(identity, "实际哈希已存在或等待确认：不重复添加，也不接管手工种子")
                        continue
                    fresh_reason, fresh_slots, fresh_budget = self._gate(self._sample(qb), rows)
                    if fresh_reason:
                        self._reason(identity, fresh_reason + "；保护在实际添加前重新检查")
                        counts["skipped"] += 1
                        break
                    slots = min(slots, fresh_slots)
                    disk_budget = min(disk_budget, fresh_budget)
                    if not slots:
                        self._reason(identity, "添加前并发重新检查未通过；未向 QB 添加")
                        counts["skipped"] += 1
                        break
                    if size > disk_budget:
                        self._reason(identity, "添加前磁盘余量不足整个种子；未向 QB 添加，不缩减文件")
                        counts["skipped"] += 1
                        continue
                    if record["free_until"] <= time.time() + self.strategy["min_free_hours"] * 3600:
                        self._reason(identity, "下载种子期间 Free 窗口不足；未向 QB 添加")
                        counts["skipped"] += 1
                        continue
                    qb.category()
                    self.store.update_candidate(identity, state="uncertain", qb_hash=expected,
                                                reason="添加请求待核验：预期哈希已持久化，禁止重复请求")
                    record = {**record, "qb_hash": expected}
                    attempted = True
                    slots -= 1
                    disk_available = disk_budget
                    disk_budget -= size
                    outcome = qb.add(content, identity, self.config["downloader"].get("save_path", ""))
                    add_returned = True
                    if outcome == "rejected":
                        counts["failed"] += 1
                        slots += 1
                        disk_budget += size
                        self.store.update_candidate(identity, state="failed", qb_hash="", reason="QB 添加接口明确拒绝；未提交")
                        continue
                    row = self._confirm(qb, record)
                    if row is None:
                        counts["uncertain"] += 1
                        continue
                    counts["submitted"] += 1
                    started_size = self._prepare(qb, row, record, disk_available)
                    if started_size:
                        counts["started"] += 1
                        disk_budget += size - started_size
                    else:
                        counts["skipped"] += 1
                        slots += 1
                        disk_budget += size
                except IntegrationError as error:
                    counts["failed"] += 1
                    if attempted:
                        # Once confirmed, preserve ownership even if file setup/start fails.
                        current = next(c for c in self.store.candidates(limit=100000) if c["id"] == identity)
                        if current["state"] == "submitted":
                            self._reason(identity, "待准备：" + str(error))
                        elif add_returned or error.ambiguous or error.status is None:
                            counts["uncertain"] += 1
                            self._reason(identity, str(error) + "；结果不确定，保留哈希等待核对，不重发", state="uncertain")
                        else:
                            slots += 1
                            disk_budget += size
                            self.store.update_candidate(identity, state="failed", qb_hash="", reason=self.store.redact(str(error)))
                    else:
                        self._reason(identity, str(error) + "；尚未向 QB 添加，继续处理其他候选", state="failed")
                    self._event("ERROR", "brush.candidate", str(error), {"id": identity, "http_status": error.status, "add_attempted": attempted})
            status = "partial" if counts["uncertain"] or (counts["failed"] and (counts["submitted"] or counts["reconciled"])) else "failed" if counts["failed"] else "succeeded" if counts["submitted"] or counts["reconciled"] else "skipped"
            message = f"实际哈希确认 {counts['submitted']} 项，核对恢复 {counts['reconciled']} 项，启动请求 {counts['started']} 项，不确定 {counts['uncertain']} 项，失败 {counts['failed']} 项"
            self._event("INFO", "brush", message, counts)
            return result(status, message, **counts)
        finally:
            qb.close()
            if mt is not None:
                mt.close()

    def cleanup(self):
        self._event("INFO", "cleanup", "检查已确认所有权；Free 保护不删除数据")
        qb = self._qb()
        counts = {"protected": 0, "deleted": 0, "failed": 0, "skipped": 0}
        try:
            rows = qb.torrents()
            self._observe(rows)
            counts["protected"], counts["failed"] = self._expire(qb, rows)
            if not self.strategy["cleanup_enabled"]:
                return result("partial" if counts["failed"] else "succeeded" if counts["protected"] else "skipped",
                              "闲置清理未启用；Free 下载保护仍生效，保留全部数据", **counts)
            for row, record in owned(qb.torrents(), self.store.candidates(limit=100000)):
                now = time.time()
                activity = record.get("last_activity") or now
                added = max(record.get("submitted_at") or now, row.get("added_on") or 0)
                completion = row.get("completion_on", 0)
                if completion > 0:
                    added = max(added, completion)
                if (row.get("upspeed", 0) > 0 or row.get("dlspeed", 0) > 0
                        or row.get("state") not in STOPPED | {"stalledUP", "stalledDL", "queuedUP", "queuedDL"}
                        or now - activity < self.strategy["idle_minutes"] * 60
                        or now - added < self.strategy["protect_seed_hours"] * 3600):
                    counts["skipped"] += 1
                    continue
                try:
                    qb.delete(row["hash"], self.strategy["delete_files"])
                    if any(r["hash"].lower() == row["hash"].lower() for r in qb.torrents()):
                        self._reason(record["id"], "清理接口成功但种子仍存在；未标记已删除")
                        counts["failed"] += 1
                        continue
                    self.store.update_candidate(record["id"], state="removed", reason="已核实 QB 种子移除；" + ("请求同时删除文件" if self.strategy["delete_files"] else "保留下载文件"))
                    counts["deleted"] += 1
                    self._event("INFO", "cleanup.delete", "已核实种子从 QB 消失", {
                        "id": record["id"], "hash": row["hash"], "delete_files": self.strategy["delete_files"],
                        "idle_seconds": now - activity, "protected_age_seconds": now - added,
                    })
                except IntegrationError as error:
                    counts["failed"] += 1
                    self._reason(record["id"], str(error) + "；清理结果未确认，保留所有权记录")
            return result("partial" if counts["failed"] else "succeeded", f"确认移除 {counts['deleted']} 项；Free 保护 {counts['protected']} 项", **counts)
        finally:
            qb.close()

    def test_connection(self, target):
        if target == "downloader":
            qb = self._qb()
            try:
                qb.sample()
                qb.torrents()
                return {"ok": True, "message": "qBittorrent 认证、全局统计和种子列表读取成功", "version": qb.version}
            finally:
                qb.close()
        if target == "mteam":
            mt = self._mt()
            try:
                modes = self.config["mteam"]["modes"]
                if not modes:
                    raise IntegrationError("M-Team 至少需要启用一个查询分类")
                rows = mt.search(modes[0], 1)
                return {"ok": True, "message": "M-Team API 认证及真实查询成功", "returned": len(rows)}
            finally:
                mt.close()
        raise IntegrationError("未知连接测试目标")
