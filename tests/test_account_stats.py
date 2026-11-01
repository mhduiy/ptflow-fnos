"""Daily account state and owned-capacity consumer regressions over real HTTP."""
from datetime import datetime
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ptflow.config import Config
from ptflow.engine import Engine, MIB
from ptflow.scheduler import Scheduler
from ptflow.store import Store
from ptflow.web import create_app
from test_engine import Simulator


def timestamp(value, zone="America/New_York"):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp()


class AccountStatsTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"SOCKET_PATH": "/tmp/ptflow-test-socket", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sim = Simulator()
        self.addCleanup(self.sim.close)
        self.config = Config(self.directory.name)
        self.config.save({"mteam": {"url": self.sim.url, "api_key": "account-test-key"},
                          "schedule": {"timezone": "America/New_York", "work_start": "01:00", "work_end": "01:01"}})
        self.store = Store(self.directory.name)
        self.store.secrets = self.config.secrets()

    def engine(self):
        return Engine(self.config.get(), self.store, lambda *args: None)

    def stats(self, at, days=90):
        active = self.config.get()
        return self.store.account_stats(active["mteam"], active["schedule"]["timezone"], days, at)

    def sample(self, at):
        with patch("time.time", return_value=at):
            return self.engine().account()

    def owned_record(self, identity, *, state="submitted", category="ptflow", tags=None):
        torrent_hash = f"{int(identity):040x}"
        self.store.upsert_candidate({"id": identity, "name": "owned", "size": 32 * MIB, "seeders": 2,
                                     "leechers": 3, "free_until": time.time() + 86400, "score": 1, "mode": "movie"})
        self.store.update_candidate(identity, state=state, qb_hash=torrent_hash)
        self.sim.rows[torrent_hash] = {"hash": torrent_hash, "category": category,
                                      "tags": tags if tags is not None else f"ptflow-id-{identity}",
                                      "name": "owned", "size": 5 * MIB, "progress": 0, "state": "downloading",
                                      "upspeed": 0, "dlspeed": 1, "uploaded": 0, "downloaded": 0, "ratio": 0,
                                      "added_on": 0, "completion_on": 0, "last_activity": 0}
        return torrent_hash

    def test_calendar_sampling_survives_restart_and_uses_dst_midnight(self):
        first = timestamp("2026-03-07T23:50:00")
        self.assertEqual(self.sample(first)["status"], "succeeded")
        before = self.stats(first)
        self.assertEqual(before["next_at"], timestamp("2026-03-08T00:00:00"))
        self.assertEqual(before["latest"]["day"], "2026-03-07")
        self.store = Store(self.directory.name)
        self.store.secrets = self.config.secrets()
        with patch("time.time", return_value=first + 60):
            restarted = Scheduler(self.config, self.store)
            self.assertEqual(restarted.deadlines["account"], before["next_at"])
            self.assertEqual(self.engine().account()["status"], "skipped")
        second = timestamp("2026-03-08T00:01:00")
        self.assertEqual(self.sample(second)["status"], "succeeded")
        after = self.stats(second)
        self.assertEqual(after["next_at"], timestamp("2026-03-09T00:00:00"))
        self.assertEqual(after["next_at"] - before["next_at"], 23 * 3600)
        self.assertEqual([row["day"] for row in after["history"]], ["2026-03-07", "2026-03-08"])
        self.assertEqual(len(self.stats(second, 1)["history"]), 1)
        self.assertEqual(self.sample(second + 30)["status"], "skipped")
        self.assertEqual(self.sim.calls.count("/api/member/profile"), 2)
        self.assertFalse(any(path.startswith("/api/v2/") for path in self.sim.calls))

    def test_request_spanning_midnight_does_not_skip_the_new_calendar_day(self):
        start = timestamp("2026-04-01T23:59:55")
        finish = timestamp("2026-04-02T00:00:05")
        clock = [start]
        self.sim.profile_delay = 0.2
        with patch("time.time", side_effect=lambda: clock[0]):
            scheduler = Scheduler(self.config, self.store)
            scheduler.submit("account", "automatic")
            deadline = time.monotonic() + 3
            while "/api/member/profile" not in self.sim.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            clock[0] = finish
            scheduler.worker.join(5)
            self.assertFalse(scheduler.worker.is_alive())
            self.assertEqual(self.stats(finish)["latest"]["day"], "2026-04-01")
            self.assertLessEqual(scheduler.deadlines["account"], finish)
            self.sim.profile_delay = 0
            scheduler.submit("account", "automatic")
            scheduler.worker.join(5)
            self.assertFalse(scheduler.worker.is_alive())
        self.assertEqual([row["day"] for row in self.stats(finish)["history"]], ["2026-04-01", "2026-04-02"])
        self.assertEqual(self.sim.calls.count("/api/member/profile"), 2)

    def test_rotation_requires_verification_and_accounts_and_origins_are_isolated(self):
        first = timestamp("2026-04-01T12:00:00")
        self.sample(first)
        self.config.save({"mteam": {"api_key": "rotated-account-key"}})
        self.assertIsNone(self.stats(first)["latest"])
        self.assertEqual(self.stats(first)["history"], [])
        self.sample(first + 1)
        self.assertEqual(len(self.stats(first + 1)["history"]), 1)
        second = timestamp("2026-04-02T12:00:00")
        self.sample(second)
        self.assertEqual(len(self.stats(second)["history"]), 2)
        self.config.save({"mteam": {"api_key": "different-account-key"}})
        self.sim.profile_data["id"] = "200"
        self.sim.profile_data["memberCount"]["bonus"] = "42.5"
        self.assertIsNone(self.stats(second)["latest"])
        self.sample(second + 1)
        self.assertEqual(self.stats(second)["latest"]["bonus"], 42.5)
        self.assertEqual(len(self.stats(second)["history"]), 1)
        other = Simulator()
        self.addCleanup(other.close)
        other.profile_data = self.sim.profile_data
        self.config.save({"mteam": {"url": other.url}})
        self.assertIsNone(self.stats(second)["latest"])
        self.sample(second + 2)
        self.assertEqual(len(self.stats(second)["history"]), 1)
        self.config.save({"mteam": {"url": self.sim.url, "api_key": "rotated-account-key"}})
        self.assertEqual(len(self.stats(second)["history"]), 2)
        self.assertEqual(self.stats(second)["latest"]["bonus"], 1000.25)

    def test_failure_keeps_success_and_attempt_deadline_after_restart(self):
        first = timestamp("2026-04-01T12:00:00")
        self.sample(first)
        latest = self.stats(first)["latest"]
        self.sim.statuses["/api/member/profile"] = 429
        second = timestamp("2026-04-02T12:00:00")
        self.assertEqual(self.sample(second)["status"], "failed")
        failed = self.stats(second)
        self.assertEqual(failed["latest"], latest)
        self.assertIn("429", failed["error"])
        self.assertEqual(failed["last_attempt_at"], second)
        self.assertEqual(failed["next_at"], timestamp("2026-04-03T00:00:00"))
        self.store = Store(self.directory.name)
        with patch("time.time", return_value=second + 60):
            self.assertEqual(Scheduler(self.config, self.store).deadlines["account"], failed["next_at"])
            self.assertEqual(self.engine().account()["status"], "skipped")
        self.config.save({"mteam": {"api_key": "unverified-failing-key"}})
        self.assertEqual(self.sample(second + 120)["status"], "failed")
        isolated = self.stats(second + 120)
        self.assertIsNone(isolated["latest"])
        self.assertEqual(isolated["history"], [])
        self.assertIn("429", isolated["error"])
        self.assertNotIn("unverified-failing-key", str(isolated))

    def test_interrupted_attempt_does_not_repeat_http_on_restart(self):
        at = timestamp("2026-04-01T12:00:00")
        self.assertTrue(self.store.begin_account_sample(self.config.get()["mteam"], "America/New_York", at))
        self.store = Store(self.directory.name)
        self.assertEqual(self.sample(at + 10)["status"], "skipped")
        self.assertTrue(self.stats(at + 10)["error"])
        self.assertEqual(self.sim.calls, [])

    def test_automatic_account_job_ignores_qb_toggle_and_work_window(self):
        self.store.finish_run({"id": "manual-feedback", "kind": "query", "source": "manual", "started_at": 1,
                               "finished_at": 2, "status": "succeeded", "message": "manual result", "counts": {}})
        scheduler = Scheduler(self.config, self.store)
        scheduler.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.stats(time.time())["latest"] is None:
                time.sleep(0.01)
            self.assertIsNotNone(self.stats(time.time())["latest"])
        finally:
            scheduler.stop()
        self.assertEqual(self.sim.calls, ["/api/member/profile"])
        self.assertGreater(scheduler.deadlines["account"], time.time())
        self.assertEqual([run["id"] for run in self.store.runs()], ["manual-feedback"])
        self.assertEqual(scheduler.status()["last_run"]["id"], "manual-feedback")

    def test_owned_capacity_uses_whole_sizes_and_excludes_other_ownership(self):
        self.config.save({"downloader": {"url": self.sim.url, "username": "simulated-admin", "password": "simulated-auth"}})
        whole = self.owned_record("1")
        self.sim.rows[whole]["total_size"] = 64 * MIB
        self.owned_record("2")  # Older QB: persisted verified size, not selected size.
        self.owned_record("3", category="manual")
        self.owned_record("4", tags="ptflow-id-999")
        self.owned_record("5", state="uncertain")
        self.owned_record("6", state="pending")
        external = f"{7:040x}"
        self.sim.rows[external] = {**self.sim.rows[whole], "hash": external, "tags": "ptflow-id-7"}
        items = self.engine().torrents()
        self.assertEqual({row["id"] for row in items}, {"1", "2"})
        self.assertEqual(sum(row["size"] for row in items), 96 * MIB)

    def test_slow_account_http_keeps_free_protection_running(self):
        self.config.save({"downloader": {"url": self.sim.url, "username": "simulated-admin", "password": "simulated-auth"}})
        torrent_hash = self.owned_record("1")
        self.store.update_candidate("1", last_activity=time.time(), submitted_at=time.time())
        with self.store.connection() as db:
            db.execute("UPDATE candidates SET free_until=? WHERE id='1'", (time.time() + 1,))
        self.sim.files[torrent_hash] = [{"index": 0, "size": 32 * MIB, "priority": 1, "progress": 0}]
        self.sim.profile_delay = 0.4
        active = self.config.get()
        active["schedule"]["stats_seconds"] = 0.05  # Shorten only the test's protection cadence.
        completed = []
        worker = threading.Thread(target=lambda: completed.append(Engine(active, self.store, lambda *args: None).account()))
        worker.start()
        try:
            self.assertTrue(self.sim.stopped.wait(2))
            self.assertTrue(worker.is_alive())
        finally:
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(completed[0]["status"], "succeeded")
        self.assertEqual(self.sim.files[torrent_hash][0]["priority"], 0)
        self.assertEqual(self.sim.adds, [])

    def test_endpoint_is_authenticated_bounded_and_cache_only(self):
        at = time.time()
        self.sample(at)
        app = create_app(self.directory.name, start_scheduler=False)
        client = app.test_client()
        self.assertEqual(client.get("/api/mteam/stats").status_code, 401)
        token = client.get("/api/auth").json["csrf_token"]
        response = client.post("/api/setup", json={"password": "account-stats-test-password"},
                               headers={"Origin": "http://localhost", "X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        calls = list(self.sim.calls)
        response = client.get("/api/mteam/stats?days=90")
        self.assertEqual(response.status_code, 200)
        latest = response.json["latest"]
        self.assertEqual((latest["bonus"], latest["uploaded"], latest["downloaded"], latest["ratio"]),
                         (1000.25, 5 * 1024**3, 1024**3, 5.0))
        self.assertEqual(response.json["history"], [latest])
        for invalid in ("0", "3651", "no", "1.5"):
            self.assertEqual(client.get(f"/api/mteam/stats?days={invalid}").status_code, 400)
        self.assertEqual(self.sim.calls, calls)


if __name__ == "__main__":
    unittest.main()
