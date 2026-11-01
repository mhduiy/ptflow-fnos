"""Consumer-visible configuration failure, restart, and retained-data boundaries."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import errno
import json
import time
import uuid

from ptflow.config import Config, atomic_json, torrent_size_bounds
from ptflow.store import Store
from ptflow.web import create_app


class PersistenceTests(TestCase):
    def test_rejected_settings_never_change_saved_or_live_configuration(self):
        invalid = (
            {"downloader": {"url": "   "}, "mteam": {"api_key": uuid.uuid4().hex}, "schedule": {"enabled": True}},
            {"strategy": {"max_downloading": 10**500}},
            {"strategy": {"max_torrent_gib": float("inf")}},
            {"strategy": {"min_free_gib": float("nan")}},
            {"strategy": {"max_downloading": True}},
            {"strategy": {"max_downloading": 1.5}},
            {"strategy": {"max_torrent_gib": 0.001}},
            {"strategy": {"max_torrent_gib": 100001}},
            {"strategy": {"min_torrent_gib": -0.01}},
            {"strategy": {"min_torrent_gib": 100001}},
            {"strategy": {"min_torrent_gib": 11}},
            {"strategy": {"min_torrent_gib": 2, "max_torrent_gib": 1}},
            {"strategy": {"min_torrent_gib": float("nan")}},
            {"strategy": {"min_torrent_gib": float("inf")}},
            {"strategy": {"min_torrent_gib": True}},
            {"strategy": {"min_torrent_gib": "1"}},
            {"strategy": {"min_torrent_gib": None}},
            {"strategy": {"max_torrent_gib": float("nan")}},
            {"strategy": {"max_torrent_gib": False}},
            {"strategy": {"max_torrent_gib": "10"}},
            {"mteam": {"request_interval_seconds": 0}},
            {"mteam": {"page_size": 201}},
            {"mteam": {"modes": []}},
            {"mteam": {"modes": ["movie", "movie"]}},
            {"mteam": {"url": "http://example.com"}},
            {"downloader": {"url": "https://user:password@example.com"}},
            {"downloader": {"url": "https://example.com:99999"}},
            {"downloader": {"url": "https://example.com/?token=private"}},
            {"schedule": {"work_start": "25:00"}},
            {"schedule": {"timezone": "Not/A_Timezone"}},
            {"schedule": {"stats_seconds": 4}},
            {"strategy": {"idle_minutes": 4}},
        )
        with TemporaryDirectory() as directory:
            config = Config(directory)
            original, original_bytes = config.get(), config.path.read_bytes()
            for value in invalid:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        config.save(value)
                    self.assertEqual(config.get(), original)
                    self.assertEqual(config.path.read_bytes(), original_bytes)
            self.assertEqual(config.save({"downloader": {"url": "   "}})["downloader"]["url"], "")
            result = config.save({"strategy": {"max_torrent_gib": 100000, "min_free_gib": 0}})
            self.assertEqual(result["strategy"]["max_torrent_gib"], 100000)
            self.assertEqual(Config(directory).get()["strategy"]["min_free_gib"], 0)

    def test_legacy_settings_add_minimum_without_resetting_saved_values(self):
        with TemporaryDirectory() as directory:
            config = Config(directory)
            config.save({"strategy": {"max_torrent_gib": 37.5},
                         "mteam": {"proxy_url": "http://127.0.0.1:7890", "api_key": uuid.uuid4().hex},
                         "downloader": {"auth_type": "api_key", "api_key": uuid.uuid4().hex}})
            legacy, auth = config.get(), config.auth.copy()
            del legacy["strategy"]["min_torrent_gib"]
            atomic_json(config.path, legacy)
            reopened = Config(directory)
            self.assertEqual(reopened.get(), {**legacy, "strategy": {**legacy["strategy"], "min_torrent_gib": 0}})
            self.assertEqual(reopened.auth, auth)
            self.assertEqual(reopened.public()["mteam"]["api_key"], "")
            reopened.save({"schedule": {"query_minutes": 47}})
            self.assertEqual(json.loads(config.path.read_text())["strategy"]["min_torrent_gib"], 0)
            self.assertEqual(Config(directory).get()["strategy"]["max_torrent_gib"], 37.5)

    def test_total_size_limits_allow_equality_and_round_inward(self):
        gib = 2**30
        with TemporaryDirectory() as directory:
            config = Config(directory)
            self.assertEqual(torrent_size_bounds(config.get()["strategy"]), (0, 10 * gib))
            config.save({"strategy": {"min_torrent_gib": 2, "max_torrent_gib": 2}})
            self.assertEqual(torrent_size_bounds(Config(directory).get()["strategy"]), (2 * gib, 2 * gib))
            config.save({"strategy": {"min_torrent_gib": 1 + 0.25 / gib, "max_torrent_gib": 2 + 0.75 / gib}})
            self.assertEqual(torrent_size_bounds(config.get()["strategy"]), (gib + 1, 2 * gib))
            config.save({"strategy": {"min_torrent_gib": 0, "max_torrent_gib": 0.01}})
            self.assertEqual(torrent_size_bounds(config.get()["strategy"]), (0, 10737418))

    def test_failed_settings_write_preserves_restart_state_and_private_files(self):
        with TemporaryDirectory() as directory:
            config = Config(directory)
            config.save({"downloader": {"password": uuid.uuid4().hex}, "strategy": {"max_torrent_gib": 0.01}})
            original, original_bytes = config.get(), config.path.read_bytes()
            with patch("ptflow.config.os.replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
                with self.assertRaises(OSError):
                    config.save({"strategy": {"max_torrent_gib": 25}})
            self.assertEqual(config.get(), original)
            self.assertEqual(Config(directory).get(), original)
            self.assertEqual(config.path.read_bytes(), original_bytes)
            self.assertEqual(config.path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(Path(directory).glob(".config.json.*")))
            self.assertEqual(config.public()["downloader"]["password"], "")

    def test_failed_password_write_preserves_sessions_and_restart_login(self):
        with TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            app = create_app(directory, start_scheduler=False)
            first, second = app.test_client(), app.test_client()
            old_password, new_password = uuid.uuid4().hex, uuid.uuid4().hex
            origin = {"Origin": "http://localhost"}
            token = first.get("/api/auth").json["csrf_token"]
            response = first.post("/api/setup", json={"password": old_password}, headers={**origin, "X-CSRF-Token": token})
            self.assertEqual(response.status_code, 200)
            token = response.json["csrf_token"]
            second_token = second.get("/api/auth").json["csrf_token"]
            response = second.post("/api/login", json={"password": old_password}, headers={**origin, "X-CSRF-Token": second_token})
            self.assertEqual(response.status_code, 200)
            with patch("ptflow.config.os.replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
                response = first.post("/api/password", json={"current_password": old_password, "password": new_password}, headers={**origin, "X-CSRF-Token": token})
            self.assertEqual(response.status_code, 500)
            self.assertIn("No space left", response.json["error"])
            self.assertEqual(first.get("/api/config").status_code, 200)
            self.assertEqual(second.get("/api/config").status_code, 200)
            restarted = create_app(directory, start_scheduler=False).test_client()
            token = restarted.get("/api/auth").json["csrf_token"]
            response = restarted.post("/api/login", json={"password": old_password}, headers={**origin, "X-CSRF-Token": token})
            self.assertEqual(response.status_code, 200)
            token = response.json["csrf_token"]
            response = restarted.post("/api/login", json={"password": new_password}, headers={**origin, "X-CSRF-Token": token})
            self.assertEqual(response.status_code, 401)

    def test_refresh_and_restart_preserve_confirmed_and_uncertain_ownership(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            for identifier, state in (("1", "submitted"), ("2", "uncertain")):
                store.upsert_candidate({"id": identifier, "name": "candidate-" + identifier, "size": 4096,
                                        "seeders": 2, "leechers": 3, "free_until": time.time() + 86400,
                                        "score": 5, "mode": "movie"})
                store.update_candidate(identifier, state=state, qb_hash=identifier * 40, submitted_at=123, last_activity=456)
            restarted = Store(directory)
            for row in restarted.candidates():
                restarted.upsert_candidate({**row, "name": "refreshed-" + row["id"], "leechers": 8})
            rows = {row["id"]: row for row in Store(directory).candidates()}
            self.assertEqual((rows["1"]["state"], rows["2"]["state"]), ("submitted", "uncertain"))
            for identifier, row in rows.items():
                self.assertEqual((row["qb_hash"], row["submitted_at"], row["last_activity"]), (identifier * 40, 123, 456))
                self.assertEqual(row["leechers"], 8)

    def test_candidate_size_filter_runs_before_limit_and_size_correction_persists(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            base = {"name": "candidate", "seeders": 2, "leechers": 3, "free_until": time.time() + 86400,
                    "score": 1, "mode": "movie"}
            for identifier, size, score in (("small", 99, 100), ("large", 201, 99), ("minimum", 100, 2), ("maximum", 200, 1)):
                store.upsert_candidate({**base, "id": identifier, "size": size, "score": score})
            self.assertEqual([row["id"] for row in store.candidates(limit=2, minimum_size=100, maximum_size=200)],
                             ["minimum", "maximum"])
            self.assertEqual(store.candidates(limit=1, minimum_size=100)[0]["id"], "large")
            self.assertEqual(store.candidates(limit=1, maximum_size=200)[0]["id"], "small")
            self.assertEqual(len(store.candidates()), 4)
            store.update_candidate("large", size=150, state="submitted", qb_hash="a" * 40)
            restarted = Store(directory)
            self.assertEqual(restarted.candidates(limit=1, minimum_size=100, maximum_size=200)[0]["size"], 150)
            self.assertEqual(restarted.prune_candidates(100, 200), 1)
            self.assertEqual({row["id"] for row in restarted.candidates()}, {"large", "minimum", "maximum"})
            self.assertEqual(restarted.prune_candidates(100, 200), 0)

    def test_all_retained_events_remain_readable_at_pruning_boundary(self):
        with TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            store = Store(directory)
            with store.connection() as db:
                db.executemany("INSERT INTO events(time,level,stage,message,run_id,details) VALUES(?,?,?,?,?,?)",
                               (("2026-01-01T00:00:00Z", "INFO", "retention", "event-" + str(index), "", "{}") for index in range(1, 10100)))
            self.assertEqual([row["id"] for row in store.events(limit=10099)], list(range(1, 10100)))
            store.event("INFO", "retention", "event-10100")
            self.assertEqual(store.event_bounds(), (101, 10100))
            self.assertEqual([row["id"] for row in store.events(limit=10099)], list(range(101, 10101)))

    def test_history_retention_and_downsampling_preserve_window_endpoints(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            now = time.time()
            first = now - 5 * 4999
            with store.connection() as db:
                db.executemany("INSERT INTO samples VALUES(?,?,?,?,?,?)",
                               ((first + index * 5, index, index * 2, index * 3, index * 4, 100000) for index in range(5000)))
                db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?)", (now - 8 * 86400, 0, 0, 0, 0, 1))
            store.sample({"time": now, "upspeed": 7, "dlspeed": 9, "uploaded": 11, "downloaded": 13, "free_space": 100000})
            rows = Store(directory).history(10080)
            self.assertLessEqual(len(rows), 1000)
            self.assertEqual(rows[0]["time"], first)
            self.assertEqual((rows[-1]["time"], rows[-1]["uploaded"]), (now, 11))
            self.assertEqual(Store(directory).latest_sample()["dlspeed"], 9)
