"""Single-flight jobs with editable intervals and visible execution outcomes."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event, RLock, Thread
import time
import uuid

from . import __version__
from .config import is_work_time
from .engine import Engine

LABELS = {"query": "种子查询", "brush": "刷流补充", "cleanup": "安全维护", "collect": "状态采样", "full": "立即执行一次"}


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds") if timestamp is not None else None


class Scheduler:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = RLock()
        self.wakeup, self.stopping = Event(), Event()
        self.current = None
        self.worker = None
        self.deadlines = {}
        self.reschedule()
        self.thread = Thread(target=self._loop, name="ptflow-scheduler", daemon=True)

    def intervals(self, config=None):
        schedule = (config or self.config.get())["schedule"]
        return {"query": schedule["query_minutes"] * 60, "brush": schedule["brush_minutes"] * 60,
                "cleanup": schedule["cleanup_minutes"] * 60, "collect": schedule["stats_seconds"]}

    def reschedule(self):
        now = time.time()
        with self.lock:
            self.deadlines = {key: now + duration for key, duration in self.intervals().items()}
            self.deadlines["collect"] = now
        self.wakeup.set()

    def start(self):
        self.store.event("INFO", "系统", "PTFlow 已启动；默认暂停自动刷流，填写配置并测试连接后开启", details={"automatic": self.config.get()["schedule"]["enabled"]})
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wakeup.set()
        self.thread.join(timeout=2)
        if self.worker:
            self.worker.join(timeout=2)

    def _loop(self):
        while not self.stopping.is_set():
            config = self.config.get()
            now = time.time()
            with self.lock:
                due = [key for key in ("collect", "query", "brush", "cleanup") if self.deadlines[key] <= now]
                active = self.current is not None
            for kind in due:
                if active:
                    break  # overdue work is retried after the current run, never piled into a queue
                if not config["downloader"]["url"] or (kind != "collect" and not config["schedule"]["enabled"]):
                    with self.lock:
                        self.deadlines[kind] = now + self.intervals(config)[kind]
                    continue
                if kind in ("query", "brush") and not is_work_time(config):
                    self.store.event("INFO", "调度", f"跳过{LABELS[kind]}：当前不在工作时段；未查询站点或新增 QB 任务", details={"start": config["schedule"]["work_start"], "end": config["schedule"]["work_end"], "timezone": config["schedule"]["timezone"]})
                    with self.lock:
                        self.deadlines[kind] = now + self.intervals(config)[kind]
                    continue
                try:
                    self.submit(kind, "automatic")
                    active = True
                except RuntimeError:
                    break
            self.wakeup.wait(1)
            self.wakeup.clear()

    def submit(self, kind, source="manual"):
        if not isinstance(kind, str) or kind not in LABELS or kind == "collect" and source == "manual":
            raise ValueError("不支持的任务类型")
        with self.lock:
            if self.current is not None:
                raise RuntimeError("已有任务运行中，请查看当前阶段和实时日志；不会重复启动")
            if self.stopping.is_set():
                raise RuntimeError("应用正在停止，不能启动任务")
            identifier = uuid.uuid4().hex[:16]
            self.current = {"id": identifier, "kind": kind, "source": source, "stage": "准备", "message": "任务已接收，等待执行", "started_at": time.time()}
            if kind == "full":
                for key in ("query", "brush", "cleanup", "collect"):
                    self.deadlines[key] = time.time() + self.intervals()[key]
            else:
                self.deadlines[kind] = time.time() + self.intervals()[kind]
            self.worker = Thread(target=self._execute, args=(deepcopy(self.current),), name=f"ptflow-{kind}", daemon=True)
            self.worker.start()
            return identifier

    def _execute(self, run):
        def progress(stage, message):
            if self.stopping.is_set():
                raise InterruptedError("应用停止，当前任务中断；已确认提交的种子记录保留")
            with self.lock:
                if self.current and self.current["id"] == run["id"]:
                    self.current.update(stage=stage, message=self.store.redact(message))

        counts, results = {}, []
        with self.store.run_context(run["id"]):
            self.store.event("INFO", "任务", f"开始{LABELS[run['kind']]}；来源={'手动' if run['source'] == 'manual' else '定时'}", details={"manual_safety": "手动执行仅跳过自动开关和工作时段，其他保护保持生效"})
            try:
                engine = Engine(self.config.get(), self.store, progress)
                kinds = ("query", "collect", "brush", "cleanup") if run["kind"] == "full" else (run["kind"],)
                for kind in kinds:
                    progress(LABELS[kind], f"正在执行{LABELS[kind]}")
                    result = getattr(engine, kind)()
                    results.append(result)
                    counts[kind] = result.get("counts", {})
                    self.store.event("INFO" if result["status"] == "succeeded" else "WARNING", LABELS[kind], result["message"], details={"status": result["status"], "counts": result.get("counts", {})})
                statuses = {result["status"] for result in results}
                status = "failed" if statuses == {"failed"} else "partial" if statuses & {"failed", "partial"} else "skipped" if statuses == {"skipped"} else "succeeded"
                message = "；".join(result["message"] for result in results)
            except Exception as error:
                status = "failed"
                stage = self.current["stage"] if self.current else "未知阶段"
                message = self.store.redact(f"{stage}失败：{type(error).__name__}：{error}")
                self.store.event("ERROR", stage, message, details={"counts": counts, "next": "检查本阶段错误与连接测试；未确认的 QB 提交不会被当作成功"})
            finally:
                run.update(finished_at=time.time(), status=status, message=message, counts=counts)
                self.store.finish_run(run)
                self.store.event("INFO" if status == "succeeded" else "WARNING", "任务", f"{LABELS[run['kind']]}结束：{status}；{message}", details={"seconds": round(run["finished_at"] - run["started_at"], 2), "counts": counts})
                with self.lock:
                    self.current = None
                self.wakeup.set()

    def status(self):
        config = self.config.get()
        enabled = config["schedule"]["enabled"]
        sample = self.store.latest_sample()
        runs = self.store.runs(1)
        with self.lock:
            current = deepcopy(self.current)
            schedules = [{"kind": key, "label": LABELS[key], "interval_seconds": duration, "next_at": iso(self.deadlines[key]) if (key == "collect" and config["downloader"]["url"] or enabled and config["downloader"]["url"]) else None} for key, duration in self.intervals(config).items()]
        return {"version": __version__, "configured": bool(config["downloader"]["url"] and config["mteam"]["api_key"]),
                "enabled": enabled, "working_time": is_work_time(config), "now": iso(time.time()),
                "current_run": current, "last_run": runs[0] if runs else None, "schedules": schedules,
                "sample": sample, "sample_age_seconds": max(0, time.time() - sample["time"]) if sample else None,
                "summary": self.store.summary(), "timezone": config["schedule"]["timezone"]}
