"""Runnable real-HTTP regression checks: python -m unittest discover -s tests."""

from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ptflow.config import DEFAULTS
from ptflow.engine import Engine, GIB, MIB, candidate, owned
from ptflow.integrations import IntegrationError, add_outcome, select_files, torrent_metadata
from ptflow.store import Store


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in value.items()) + b"e"


def torrent(identity="1"):
    lengths = [5 * MIB, 7 * MIB, 20 * MIB]
    # Intentionally non-sorted keys: info hashing must preserve bytes, not re-encode.
    info = {b"name": f"seed-{identity}".encode(), b"pieces": b"x" * (sum(lengths) // MIB * 20),
            b"piece length": MIB, b"files": [{b"length": size, b"path": [f"{i}.bin".encode()]}
                                            for i, size in enumerate(lengths)]}
    raw_info = bencode(info)
    return bencode({b"announce": b"https://tracker.example/announce", b"info": info}), raw_info


class Simulator:
    def __init__(self):
        self.rows = {}
        self.files = {}
        self.calls = []
        self.adds = []
        self.add_status = 200
        self.visible = True
        self.limit_download_id = None
        self.limit_add_id = None
        self.version = "v5.2.0"
        self.login_status = 200
        self.login_body = b"Ok."
        self.login_cookie = True
        self.require_sid = False
        self.invalid_confirmation = False
        self.priority_ignored = False
        self.resume_budgets = []
        self.headers = []
        self.files_status = 200
        self.search_delay = 0
        self.stopped = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, status=200, headers=None):
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = parse_qs(body.decode(errors="replace")) if "multipart" not in self.headers.get("Content-Type", "") else {}
                query = parse_qs(parsed.query)
                fixture.calls.append(path)
                fixture.headers.append((path, dict(self.headers)))
                if path == "/api/torrent/search":
                    time.sleep(fixture.search_delay)
                    rows = [{"id": str(i), "name": f"candidate-{i}", "size": 32 * MIB,
                             "status": {"discount": "FREE", "seeders": 2, "leechers": 10 - i,
                                        "discountEndTime": "2099-01-01 00:00:00"}}
                            for i in (1, 2)]
                    rows.append({"id": "3", "name": "not-free", "size": MIB,
                                 "status": {"discount": "NORMAL", "seeders": 1, "leechers": 2,
                                            "discountEndTime": "2099-01-01 00:00:00"}})
                    self.reply({"code": "SUCCESS", "data": {"data": rows}})
                elif path == "/api/torrent/genDlToken":
                    identity = data["id"][0]
                    self.reply({"code": "SUCCESS", "data": fixture.url + f"/redirect/{identity}?token=secret-download"})
                elif path.startswith("/redirect/"):
                    identity = path.rsplit("/", 1)[1]
                    self.reply(b"", 302, {"Location": f"/download/{identity}?token=secret-download"})
                elif path.startswith("/download/"):
                    identity = path.rsplit("/", 1)[1]
                    if identity == fixture.limit_download_id:
                        self.reply(b"rejected", 429, {"Retry-After": "60"})
                    else:
                        self.reply(torrent(identity)[0])
                elif path == "/api/v2/auth/login":
                    self.reply(fixture.login_body, fixture.login_status,
                               {"Set-Cookie": "SID=secret-cookie; Path=/"} if fixture.login_cookie else {})
                elif path == "/api/v2/app/version":
                    if fixture.require_sid and "SID=secret-cookie" not in self.headers.get("Cookie", ""):
                        self.reply(b"Forbidden", 403)
                        return
                    self.reply(fixture.version.encode())
                elif path == "/api/v2/sync/maindata":
                    self.reply({"server_state": {"up_info_speed": 0, "dl_info_speed": 0,
                                                 "alltime_ul": 1234, "alltime_dl": 5678,
                                                 "free_space_on_disk": 100 * GIB}})
                elif path == "/api/v2/torrents/info":
                    if fixture.invalid_confirmation and fixture.adds:
                        self.reply({"invalid": True})
                    else:
                        self.reply(list(fixture.rows.values()) if fixture.visible else [])
                elif path == "/api/v2/torrents/categories":
                    self.reply({"ptflow": {"name": "ptflow", "savePath": "/qb/path"}})
                elif path == "/api/v2/torrents/add":
                    msg = BytesParser(policy=default).parsebytes(
                        b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
                    parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                             for part in msg.iter_parts()}
                    tag = parts["tags"].decode()
                    identity = tag.removeprefix("ptflow-id-")
                    fixture.adds.append(identity)
                    if identity == fixture.limit_add_id:
                        self.reply(b"rate limit", 429, {"Retry-After": "15"})
                        return
                    metadata = torrent_metadata(parts["torrents"])
                    torrent_hash = metadata["hash"]
                    stopped = parts.get("stopped", parts.get("paused")) == b"true"
                    fixture.rows[torrent_hash] = {
                        "hash": torrent_hash, "category": parts["category"].decode(), "tags": tag,
                        "name": f"seed-{identity}", "size": metadata["size"], "progress": 0,
                        "state": "stoppedDL" if stopped else "downloading", "upspeed": 0, "dlspeed": 0,
                        "uploaded": 0, "downloaded": 0, "ratio": 0, "added_on": time.time(),
                        "completion_on": 0, "last_activity": 0,
                    }
                    fixture.files[torrent_hash] = [{"index": i, "size": n, "priority": 1, "progress": 0}
                                                  for i, n in enumerate(metadata["lengths"])]
                    self.reply({"success_count": 1, "pending_count": 0, "failure_count": 0,
                                "added_torrent_ids": [torrent_hash]}, fixture.add_status)
                elif path == "/api/v2/torrents/files":
                    self.reply(fixture.files[query["hash"][0]], fixture.files_status)
                elif path == "/api/v2/torrents/filePrio":
                    indexes = set(map(int, data["id"][0].split("|")))
                    if not fixture.priority_ignored:
                        for file in fixture.files[data["hash"][0]]:
                            if file["index"] in indexes:
                                file["priority"] = int(data["priority"][0])
                    self.reply(b"")
                elif path in ("/api/v2/torrents/start", "/api/v2/torrents/resume"):
                    torrent_hash = data["hashes"][0]
                    size = sum(f["size"] for f in fixture.files[torrent_hash] if f["priority"])
                    fixture.resume_budgets.append(size)
                    fixture.rows[torrent_hash]["state"] = "downloading" if size else "stalledUP"
                    self.reply(b"")
                elif path in ("/api/v2/torrents/stop", "/api/v2/torrents/pause"):
                    fixture.rows[data["hashes"][0]]["state"] = "stoppedDL"
                    fixture.rows[data["hashes"][0]]["dlspeed"] = 0
                    fixture.stopped.set()
                    self.reply(b"")
                elif path == "/api/v2/torrents/delete":
                    fixture.rows.pop(data["hashes"][0], None)
                    self.reply(b"")
                else:
                    self.reply(b"not found", 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class EngineTests(unittest.TestCase):
    def setUp(self):
        proxy_bypass = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        proxy_bypass.start()
        self.addCleanup(proxy_bypass.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.sim = Simulator()
        self.config = deepcopy(DEFAULTS)
        self.config["downloader"].update(url=self.sim.url, username="admin", password="secret-password")
        self.config["mteam"].update(url=self.sim.url, api_key="secret-mteam", modes=["movie"], request_interval_seconds=0)
        self.config["strategy"].update(min_free_gib=1, max_torrent_gib=12 * MIB / GIB)
        self.store = Store(Path(self.directory.name))
        self.store.secrets = ["secret-password", "secret-mteam", "secret-api"]
        self.engine = Engine(self.config, self.store, lambda stage, message: None)

    def tearDown(self):
        self.sim.close()
        self.directory.cleanup()

    def query(self):
        outcome = self.engine.query()
        self.assertEqual(outcome["counts"]["persisted"], 2)
        self.assertTrue(all(c["state"] == "pending" for c in self.store.candidates()))

    def test_hash_is_original_info_bytes_and_budget_is_whole_files(self):
        content, info = torrent()
        metadata = torrent_metadata(content)
        self.assertEqual(metadata["hash"], hashlib.sha1(info).hexdigest())
        with self.assertRaises(IntegrationError):
            torrent_metadata(content + b"garbage")
        files = [{"index": i, "size": n, "priority": 1} for i, n in enumerate([9, 4, 6])]
        self.assertEqual(select_files(files, 10), ([1, 2], 10))
        self.assertEqual(select_files(files, 3), ([], 0))
        self.assertEqual(add_outcome(200, b'{"success_count":0,"pending_count":0,"failure_count":1}'), "rejected")
        self.assertEqual(add_outcome(202, b'{"success_count":0,"pending_count":1,"failure_count":0}'), "uncertain")

    def test_real_query_download_confirm_and_priority_before_start(self):
        self.query()
        outcome = self.engine.brush()
        self.assertEqual(outcome["counts"]["submitted"], 2)
        self.assertEqual(self.sim.resume_budgets, [12 * MIB, 12 * MIB])
        self.assertEqual(len(self.engine.torrents()), 2)
        self.assertEqual(self.store.latest_sample()["uploaded"], 1234)
        for path, headers in self.sim.headers:
            if path.startswith(("/download/", "/redirect/")):
                self.assertNotIn("x-api-key", {k.lower(): v for k, v in headers.items()})
                self.assertNotIn("Cookie", headers)
        self.assertLess(self.sim.calls.index("/api/v2/torrents/filePrio"), self.sim.calls.index("/api/v2/torrents/start"))
        logs = json.dumps(self.store.events(limit=1000), ensure_ascii=False)
        for secret in ("secret-download", "secret-password", "secret-mteam", "secret-cookie"):
            self.assertNotIn(secret, logs)

    def test_http_429_does_not_submit_and_later_candidate_proceeds(self):
        self.query()
        self.sim.limit_download_id = "1"
        outcome = self.engine.brush()
        records = {c["id"]: c for c in self.store.candidates()}
        self.assertEqual(self.sim.adds, ["2"])
        self.assertEqual(records["1"]["state"], "failed")
        self.assertFalse(records["1"]["qb_hash"])
        self.assertEqual(records["2"]["state"], "submitted")
        self.assertEqual(outcome["status"], "partial")
        self.assertTrue(any(e["details"].get("http_status") == 429 for e in self.store.events(limit=1000)))

    def test_qb_429_is_rejected_and_does_not_consume_later_slot(self):
        self.query()
        self.sim.limit_add_id = "1"
        self.config["strategy"]["max_downloading"] = 1
        outcome = self.engine.brush()
        self.assertEqual(self.sim.adds, ["1", "2"])
        self.assertEqual(outcome["counts"]["submitted"], 1)
        records = {c["id"]: c for c in self.store.candidates()}
        self.assertEqual(records["1"]["state"], "failed")
        self.assertFalse(records["1"]["qb_hash"])

    def test_ambiguous_add_is_persisted_and_reconciles_without_second_add(self):
        self.query()
        self.sim.add_status = 500
        self.config["strategy"]["max_downloading"] = 1
        self.engine.brush()
        record = next(c for c in self.store.candidates() if c["id"] == "1")
        self.assertEqual(record["state"], "uncertain")
        self.assertTrue(record["qb_hash"])
        self.sim.add_status = 200
        engine_after_restart = Engine(self.config, Store(Path(self.directory.name)), lambda *args: None)
        outcome = engine_after_restart.brush()
        self.assertEqual(outcome["counts"]["reconciled"], 1)
        self.assertEqual(self.sim.adds, ["1"])
        self.assertEqual(next(c for c in self.store.candidates() if c["id"] == "1")["state"], "submitted")

    def test_bad_confirmation_never_retries_accepted_add(self):
        self.query()
        self.config["strategy"]["max_downloading"] = 1
        self.sim.invalid_confirmation = True
        self.engine.brush()
        record = next(c for c in self.store.candidates() if c["id"] == "1")
        self.assertEqual(record["state"], "uncertain")
        self.assertTrue(record["qb_hash"])
        self.sim.invalid_confirmation = False
        self.engine.brush()
        self.assertEqual(self.sim.adds, ["1"])

    def test_priority_failure_preserves_confirmed_ownership_and_stopped_data(self):
        self.query()
        self.config["strategy"]["max_downloading"] = 1
        self.sim.priority_ignored = True
        self.engine.brush()
        self.assertFalse(self.sim.resume_budgets)
        confirmed = [c for c in self.store.candidates() if c["state"] == "submitted"]
        self.assertEqual(len(confirmed), 1)
        self.assertTrue(confirmed[0]["reason"].startswith("待准备："))
        self.sim.priority_ignored = False
        self.engine.brush()
        self.assertEqual(self.sim.adds, ["1"])
        self.assertEqual(self.sim.resume_budgets, [12 * MIB])

    def test_accepted_but_invisible_stays_uncertain_until_actual_hash_appears(self):
        self.query()
        self.config["strategy"]["max_downloading"] = 1
        self.sim.visible = False
        self.engine.brush()
        self.engine.brush()
        self.assertEqual(self.sim.adds, ["1"])
        first = next(c for c in self.store.candidates() if c["id"] == "1")
        self.assertEqual(first["state"], "uncertain")
        self.assertFalse(first["submitted_at"])
        self.assertEqual(self.engine.torrents(), [])
        self.sim.visible = True
        self.engine.brush()
        self.assertEqual(self.sim.adds, ["1"])
        self.assertEqual(next(c for c in self.store.candidates() if c["id"] == "1")["state"], "submitted")

    def test_peak_speed_and_no_whole_file_budget_prevent_network_add(self):
        self.query()
        self.store.sample({"time": time.time() - 30, "upspeed": 0, "dlspeed": 100 * MIB,
                           "uploaded": 1, "downloaded": 2, "free_space": 100 * GIB})
        self.assertEqual(self.engine.brush()["status"], "skipped")
        self.assertFalse(self.sim.adds)
        self.config["strategy"]["max_download_mib"] = 200
        self.config["strategy"]["max_torrent_gib"] = 4 * MIB / GIB
        self.engine.brush()
        self.assertFalse(self.sim.adds)
        self.assertFalse(self.sim.resume_budgets)
        self.assertTrue(all(c["state"] == "pending" for c in self.store.candidates()))

    def test_ownership_free_protection_and_idle_minimum_age(self):
        self.query()
        self.engine.brush()
        records = self.store.candidates()
        first = records[0]
        torrent_hash = first["qb_hash"]
        manual = {**self.sim.rows[torrent_hash], "hash": "f" * 40, "tags": "", "name": "manual"}
        self.sim.rows[manual["hash"]] = manual
        self.assertEqual(len(owned(list(self.sim.rows.values()), records)), 2)
        self.store.update_candidate(first["id"], last_activity=time.time() - 90000, submitted_at=time.time() - 90000)
        # Update the queried expiry through the supported candidate upsert (keeps confirmed state).
        self.store.upsert_candidate({**first, "free_until": time.time() + 1200})
        self.engine.cleanup()
        self.assertTrue(all(f["priority"] == 0 for f in self.sim.files[torrent_hash]))
        self.assertIn(torrent_hash, self.sim.rows)
        self.assertIn(manual["hash"], self.sim.rows)
        self.config["strategy"].update(cleanup_enabled=True, protect_seed_hours=24, idle_minutes=5)
        self.sim.rows[torrent_hash].update(added_on=time.time() - 90000, state="stalledUP")
        # Expiry handling does not delete, and fresh confirmed seeds remain age-protected.
        self.engine.cleanup()
        self.assertNotIn(torrent_hash, self.sim.rows)
        self.assertIn(manual["hash"], self.sim.rows)
        self.assertEqual(len(self.sim.rows), 2)

    def test_password_login_versions_still_require_authenticated_api_access(self):
        self.sim.require_sid = True
        cases = ((200, b"Ok.", True, True), (204, b"", True, True),
                 (200, b"Fails.", False, False), (401, b"", False, False),
                 (204, b"", False, False))
        for status, body, cookie, accepted in cases:
            with self.subTest(status=status, cookie=cookie):
                self.sim.login_status, self.sim.login_body, self.sim.login_cookie = status, body, cookie
                if accepted:
                    self.assertTrue(self.engine.test_connection("downloader")["ok"])
                else:
                    with self.assertRaises(IntegrationError):
                        self.engine.test_connection("downloader")

    def test_api_key_is_bearer_and_older_password_version_uses_resume(self):
        self.config["downloader"].update(auth_type="api_key", api_key="secret-api")
        self.assertTrue(self.engine.test_connection("downloader")["ok"])
        self.assertNotIn("/api/v2/auth/login", self.sim.calls)
        self.assertTrue(any(headers.get("Authorization") == "Bearer secret-api" for _, headers in self.sim.headers))
        self.config["downloader"]["auth_type"] = "password"
        self.sim.version = "v4.6.7"
        self.query()
        self.engine.brush()
        self.assertIn("/api/v2/torrents/resume", self.sim.calls)
        self.assertNotIn("/api/v2/torrents/start", self.sim.calls)

    def managed_download(self, free_until):
        metadata = torrent_metadata(torrent("99")[0])
        old = time.time() - 90000
        record = {"id": "99", "name": "managed-99", "size": metadata["size"], "seeders": 2,
                  "leechers": 3, "free_until": free_until, "score": 1, "mode": "movie", "state": "submitted"}
        self.store.upsert_candidate(record)
        self.store.update_candidate("99", state="submitted", qb_hash=metadata["hash"], submitted_at=old, last_activity=old)
        self.sim.rows[metadata["hash"]] = {"hash": metadata["hash"], "category": "ptflow", "tags": "ptflow-id-99",
                                           "name": "managed-99", "state": "downloading", "progress": 0,
                                           "upspeed": 0, "dlspeed": 1, "added_on": old, "completion_on": 0,
                                           "size": metadata["size"], "uploaded": 0, "downloaded": 0, "ratio": 0,
                                           "last_activity": 0}
        self.sim.files[metadata["hash"]] = [{"index": index, "size": size, "priority": 1, "progress": 0}
                                           for index, size in enumerate(metadata["lengths"])]
        return metadata["hash"]

    def test_readded_untagged_torrent_is_never_claimed_or_deleted(self):
        torrent_hash = self.managed_download(time.time() + 86400)
        self.sim.rows[torrent_hash].update(tags="manual", state="stalledUP", progress=1, dlspeed=0)
        self.config["strategy"].update(cleanup_enabled=True, delete_files=True, protect_seed_hours=1, idle_minutes=5)
        outcome = self.engine.cleanup()
        self.assertEqual(outcome["counts"]["deleted"], 0)
        self.assertIn(torrent_hash, self.sim.rows)
        self.assertEqual(self.engine.torrents(), [])

    def test_failed_free_file_read_stops_download_and_blocks_new_adds(self):
        self.query()
        torrent_hash = self.managed_download(time.time() + 10)
        self.sim.files_status = 503
        outcome = self.engine.brush()
        self.assertEqual(self.sim.rows[torrent_hash]["state"], "stoppedDL")
        self.assertEqual(self.sim.adds, [])
        self.assertGreater(outcome["counts"]["failed"], 0)
        self.assertEqual(outcome["status"], "failed")

    def test_full_sampling_window_preserves_download_peak_and_upload_average(self):
        self.query()
        self.config["strategy"]["speed_window_minutes"] = 90
        self.config["schedule"]["stats_seconds"] = 5
        for field, rate in (("dlspeed", 100 * MIB), ("upspeed", self.config["strategy"]["target_upload_mib"] * MIB * 1003)):
            with self.subTest(field=field):
                with self.store.connection() as db:
                    db.execute("DELETE FROM samples")
                beginning = time.time() - 5005
                for index in range(1001):
                    sample = {"time": beginning + index * 5, "upspeed": 0, "dlspeed": 0,
                              "uploaded": 1, "downloaded": 2, "free_space": 100 * GIB}
                    sample[field] = rate if index == 1 else 0
                    self.store.sample(sample)
                self.assertIsNotNone(self.engine._gate(self.store.latest_sample(), [])[0])
                self.assertEqual(self.engine.brush()["status"], "skipped")
                self.assertEqual(self.sim.adds, [])

    def assert_protection_during_query(self, *, response_delay=0, request_gap=0):
        self.config["strategy"]["min_free_hours"] = 0.01
        self.config["schedule"]["stats_seconds"] = 5
        self.config["mteam"].update(modes=["movie", "normal"] if request_gap else ["movie"],
                                     request_interval_seconds=request_gap)
        self.sim.search_delay = response_delay
        torrent_hash = self.managed_download(time.time() + 37)
        finished = threading.Event()
        failures = []
        def execute():
            try:
                self.engine.query()
            except Exception as error:
                failures.append(error)
            finally:
                finished.set()
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            self.assertTrue(self.sim.stopped.wait(6), "Free protection was blocked by query")
            self.assertFalse(finished.is_set(), "Protection must run while the query is still waiting")
        finally:
            worker.join(12)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(all(file["priority"] == 0 for file in self.sim.files[torrent_hash]))
        self.assertEqual(self.sim.rows[torrent_hash]["state"], "stalledUP")

    def test_query_protects_download_during_slow_site_response(self):
        self.assert_protection_during_query(response_delay=7)

    def test_query_protects_download_during_request_interval(self):
        self.assert_protection_during_query(request_gap=7)

    def test_invalid_free_expiry_is_rejected(self):
        with self.assertRaises(ValueError):
            candidate({"id": "1", "status": {"discount": "FREE"}}, "movie", time.time())


if __name__ == "__main__":
    unittest.main()
