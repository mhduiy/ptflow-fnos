"""Scheduler consumer-boundary regressions using isolated SQLite and HTTP."""
from contextlib import redirect_stderr
from datetime import datetime
import errno
import io
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ptflow.config import Config, is_work_time
from ptflow.engine import Engine, GIB, MIB
from ptflow.scheduler import Scheduler
from ptflow.store import Store
from test_engine import Simulator


def sqlite_failure(kind):
    """Generate actual SQLite FULL/locked errors without filling the host disk."""
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(str(Path(directory) / "fault.sqlite"))
        db.execute("CREATE TABLE data(value BLOB)")
        db.commit()
        try:
            if kind == "full":
                pages = db.execute("PRAGMA page_count").fetchone()[0]
                db.execute(f"PRAGMA max_page_count={pages}")
                db.execute("INSERT INTO data VALUES(zeroblob(1048576))")
            else:
                db.execute("BEGIN EXCLUSIVE")
                other = sqlite3.connect(str(Path(directory) / "fault.sqlite"), timeout=0.01)
                try:
                    other.execute("INSERT INTO data VALUES(1)")
                finally:
                    other.close()
        except sqlite3.Error as error:
            return error
        finally:
            db.close()
    raise AssertionError("SQLite fault did not occur")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"SOCKET_PATH": "/tmp/ptflow-test-socket", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sim = Simulator()
        self.addCleanup(self.sim.close)
        self.config = Config(Path(self.directory.name))
        self.config.save({"downloader": {"url": self.sim.url, "username": "simulated-admin", "password": "simulated-auth"},
                          "mteam": {"url": self.sim.url, "api_key": "simulated-api-key", "modes": ["movie"], "request_interval_seconds": 1},
                          "strategy": {"min_free_gib": 1, "max_torrent_gib": 32 * MIB / GIB}})
        self.store = Store(Path(self.directory.name))
        self.store.secrets = self.config.secrets()
        self.scheduler = Scheduler(self.config, self.store)
        self.addCleanup(self.stop)

    def stop(self):
        self.scheduler.stopping.set()
        self.scheduler.wakeup.set()
        if self.scheduler.thread.is_alive():
            self.scheduler.thread.join(4)
        if self.scheduler.worker and self.scheduler.worker.is_alive():
            self.scheduler.worker.join(12)

    def wait_for(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("consumer condition was not reached")

    def completed(self, identifier):
        self.scheduler.worker.join(5)
        self.assertFalse(self.scheduler.worker.is_alive())
        self.assertIsNone(self.scheduler.current)
        result = self.scheduler.status()["last_run"]
        self.assertEqual(result["id"], identifier)
        return result

    def test_sqlite_begin_finish_end_failures_release_run_and_recover(self):
        for boundary in ("begin", "finish", "end"):
            for kind in ("full", "locked"):
                with self.subTest(boundary=boundary, sqlite=kind):
                    fault = sqlite_failure(kind)
                    original_event, original_finish = self.store.event, self.store.finish_run
                    fired = []

                    def event(level, stage, message, *args, **kwargs):
                        match = (boundary == "begin" and stage == "任务" and message.startswith("开始")
                                 or boundary == "end" and stage == "任务" and "结束：" in message)
                        if match and not fired:
                            fired.append(True)
                            raise fault
                        return original_event(level, stage, message, *args, **kwargs)

                    def finish(run):
                        if boundary == "finish" and not fired:
                            fired.append(True)
                            raise fault
                        return original_finish(run)

                    stderr = io.StringIO()
                    with patch.object(self.store, "event", event), patch.object(self.store, "finish_run", finish), redirect_stderr(stderr):
                        failed = self.completed(self.scheduler.submit("query"))
                    self.assertEqual(failed["status"], "failed")
                    self.assertIn(str(fault), failed["message"])
                    self.assertIn(str(fault), stderr.getvalue())
                    self.assertEqual(self.sim.adds, [])
                    # End-event failure must overwrite an already-persisted success.
                    if boundary != "finish":
                        self.assertEqual(self.store.runs(1)[0]["status"], "failed")
                    recovered = self.completed(self.scheduler.submit("query"))
                    self.assertEqual(recovered["status"], "succeeded")

    def test_persistent_write_failure_is_visible_without_killing_worker(self):
        fault = sqlite_failure("full")
        stderr = io.StringIO()
        with patch.object(self.store, "event", side_effect=fault), patch.object(self.store, "finish_run", side_effect=fault), redirect_stderr(stderr):
            result = self.completed(self.scheduler.submit("query"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("database or disk is full", stderr.getvalue())
        self.assertEqual(self.sim.calls, [])
        self.assertEqual(self.completed(self.scheduler.submit("query"))["status"], "succeeded")

    def test_manual_full_bypasses_switch_and_hours_not_disk_gate(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour = (now.hour + 2) % 24
        self.config.save({"schedule": {"work_start": f"{hour:02}:00", "work_end": f"{hour:02}:01"}})
        self.assertFalse(is_work_time(self.config.get()))
        self.assertFalse(self.config.get()["schedule"]["enabled"])
        self.sim.server_state["free_space_on_disk"] = GIB
        result = self.completed(self.scheduler.submit("full"))
        self.assertEqual(list(result["counts"]), ["query", "collect", "brush", "cleanup"])
        self.assertEqual(result["counts"]["query"]["persisted"], 2)
        self.assertEqual(result["counts"]["brush"]["submitted"], 0)
        self.assertEqual(self.sim.adds, [])

    def test_duplicate_submission_never_queues(self):
        self.sim.search_delay = 0.5
        identifier = self.scheduler.submit("query")
        self.wait_for(lambda: "/api/torrent/search" in self.sim.calls)
        with self.assertRaises(RuntimeError):
            self.scheduler.submit("brush")
        self.assertEqual(self.completed(identifier)["status"], "succeeded")
        self.assertEqual(len(self.store.runs()), 1)
        self.assertEqual(self.sim.adds, [])

    def test_stop_interrupts_request_gap_and_prevents_second_query(self):
        self.config.save({"mteam": {"modes": ["movie", "normal"], "request_interval_seconds": 9}})
        identifier = self.scheduler.submit("query")
        self.wait_for(lambda: self.scheduler.current and self.scheduler.current["stage"] == "query.wait")
        self.scheduler.stopping.set()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.scheduler.worker.join(2)
            self.assertFalse(self.scheduler.worker.is_alive())
            result = self.completed(identifier)
        self.assertEqual(result["status"], "failed")
        self.assertIn("InterruptedError", result["message"])
        self.assertEqual(self.sim.calls.count("/api/torrent/search"), 1)
        self.assertEqual(self.sim.adds, [])

    def test_stop_after_qb_acceptance_reconciles_without_second_add(self):
        self.config.save({"strategy": {"max_downloading": 1}})
        Engine(self.config.get(), self.store, lambda *args: None).query()
        self.sim.add_delay = 1
        self.scheduler.deadlines = {key: time.time() + 100 for key in self.scheduler.deadlines}
        self.scheduler.start()
        identifier = self.scheduler.submit("brush")
        self.wait_for(lambda: self.sim.adds == ["1"])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.scheduler.stop()
            result = self.completed(identifier)
        self.assertEqual(result["status"], "failed")
        first = next(record for record in self.store.candidates() if record["id"] == "1")
        self.assertEqual(first["state"], "uncertain")
        self.assertTrue(first["qb_hash"])
        self.assertEqual(self.sim.resume_sizes, [])
        self.sim.add_delay = 0
        restarted = Engine(self.config.get(), Store(Path(self.directory.name)), lambda *args: None)
        recovered = restarted.brush()
        self.assertEqual(recovered["counts"]["reconciled"], 1)
        self.assertEqual(self.sim.adds, ["1"])
        self.assertEqual(self.sim.resume_sizes, [32 * MIB])

    def test_automatic_loop_survives_skipped_event_lock_then_recovers(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour = (now.hour + 2) % 24
        self.config.save({"schedule": {"enabled": True, "work_start": f"{hour:02}:00", "work_end": f"{hour:02}:01"}})
        hit = threading.Event()
        original = self.store.event
        fault = sqlite_failure("locked")

        def event(level, stage, message, *args, **kwargs):
            if stage == "调度":
                hit.set()
                raise fault
            return original(level, stage, message, *args, **kwargs)

        self.scheduler.deadlines["query"] = 0
        self.scheduler.deadlines["collect"] = time.time() + 100
        stderr = io.StringIO()
        with patch.object(self.store, "event", event), redirect_stderr(stderr):
            self.scheduler.start()
            self.assertTrue(hit.wait(2))
            self.wait_for(lambda: self.scheduler.deadlines["query"] > time.time())
        self.assertTrue(self.scheduler.thread.is_alive())
        self.assertIn("database is locked", stderr.getvalue())
        self.config.save({"schedule": {"work_start": "00:00", "work_end": "00:00"}})
        with self.scheduler.lock:
            self.scheduler.deadlines["query"] = 0
        self.scheduler.wakeup.set()
        self.wait_for(lambda: any(run["kind"] == "query" for run in self.store.runs()))
        self.assertTrue(self.scheduler.thread.is_alive())
        self.assertEqual(self.sim.adds, [])

    def test_automatic_loop_recovers_when_database_and_stderr_are_unwritable(self):
        scheduler = self.scheduler

        class FullOutput:
            def write(self, text):
                scheduler.stopping.set()
                raise OSError(errno.ENOSPC, "No space left on device")

            def flush(self):
                raise OSError(errno.ENOSPC, "No space left on device")

        self.config.save({"schedule": {"enabled": True}})
        scheduler.deadlines = {kind: time.time() + 3600 for kind in scheduler.deadlines}
        scheduler.deadlines["query"] = 0
        with patch("ptflow.scheduler.is_work_time", return_value=False), patch.object(self.store, "event", side_effect=sqlite_failure("full")), redirect_stderr(FullOutput()):
            scheduler._loop()
        self.assertGreater(scheduler.deadlines["query"], time.time())
        self.assertEqual(self.sim.adds, [])
        scheduler.stopping.clear()
        scheduler.deadlines["query"] = 0
        scheduler.start()
        self.wait_for(lambda: any(run["kind"] == "query" and run["status"] == "succeeded" for run in self.store.runs()))
        self.assertTrue(scheduler.thread.is_alive())
        self.assertEqual(self.sim.adds, [])
