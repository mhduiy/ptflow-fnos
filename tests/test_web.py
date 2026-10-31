"""Consumer-visible security, settings and diagnostic cursor regressions."""
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo
import unittest

from ptflow.config import Config, is_work_time
from ptflow.web import create_app


class WebBehaviorTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.app = create_app(self.temporary.name, start_scheduler=False)
        self.client = self.app.test_client()
        self.origin = {"Origin": "http://localhost"}
        self.token = self.client.get("/api/auth").json["csrf_token"]

    def tearDown(self):
        self.temporary.cleanup()

    def post(self, path, value):
        return self.client.post(path, json=value, headers={**self.origin, "X-CSRF-Token": self.token})

    def setup_login(self):
        with redirect_stdout(StringIO()):
            response = self.post("/api/setup", {"password": "local-safe-password"})
        self.assertEqual(response.status_code, 200)
        self.token = response.json["csrf_token"]

    def test_auth_requires_origin_and_token_and_invalidates_other_sessions(self):
        self.assertEqual(self.client.get("/api/config").status_code, 401)
        self.assertEqual(self.client.post("/api/setup", json={"password": "local-safe-password"}).status_code, 403)
        self.setup_login()
        other = self.app.test_client()
        other_token = other.get("/api/auth").json["csrf_token"]
        response = other.post("/api/login", json={"password": "local-safe-password"}, headers={**self.origin, "X-CSRF-Token": other_token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(other.get("/api/config").status_code, 200)
        with redirect_stdout(StringIO()):
            response = self.post("/api/password", {"current_password": "local-safe-password", "password": "new-local-password"})
        self.assertEqual(response.status_code, 200)
        self.token = response.json["csrf_token"]
        self.assertEqual(other.get("/api/config").status_code, 401)
        self.assertEqual(self.client.get("/api/config").status_code, 200)

    def test_settings_preserve_private_tokens_and_reject_invalid_values(self):
        self.setup_login()
        headers = {**self.origin, "X-CSRF-Token": self.token}
        with redirect_stdout(StringIO()):
            response = self.client.put("/api/config", json={"mteam": {"api_key": "private-mteam-token"}, "schedule": {"query_minutes": 47}}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["mteam"]["api_key"], "")
            self.assertIn("mteam.api_key", response.json["secret_fields"])
            self.client.put("/api/config", json={"mteam": {"api_key": ""}}, headers=headers)
        config = self.app.extensions["ptflow_config"]
        self.assertEqual(config.get()["mteam"]["api_key"], "private-mteam-token")
        reopened = Config(self.temporary.name)
        self.assertEqual(reopened.get()["schedule"]["query_minutes"], 47)
        self.assertEqual(self.client.put("/api/config", json={"schedule": {"query_minutes": 0}}, headers=headers).status_code, 400)
        self.assertEqual(self.client.put("/api/config", json={"strategy": {"min_free_gib": float("nan")}}, headers=headers).status_code, 400)
        self.assertEqual(self.client.put("/api/config", json={"mteam": {"url": "http://untrusted.example"}}, headers=headers).status_code, 400)
        self.assertEqual(Path(self.temporary.name, "config.json").stat().st_mode & 0o777, 0o600)

    def test_logs_filter_paginate_without_missing_rows_or_leaking_secrets(self):
        self.setup_login()
        store = self.app.extensions["ptflow_store"]
        store.secrets = ["private-download-token"]
        with redirect_stdout(StringIO()):
            before = store.event("INFO", "筛选", "before")
            identifiers = [store.event("ERROR", "下载", f"HTTP 429 https://download.example/file?token=private-download-token seed={index}") for index in range(5)]
            store.event("INFO", "查询", "other")
        response = self.client.get(f"/api/logs?after={before}&limit=2&level=ERROR")
        found = [row["id"] for row in response.json["items"]]
        for _ in range(2):
            response = self.client.get(f"/api/logs?after={response.json['cursor']}&limit=2&level=ERROR")
            found.extend(row["id"] for row in response.json["items"])
        self.assertEqual(found, identifiers)
        exported = self.client.get("/api/logs/export").get_data(as_text=True)
        self.assertNotIn("private-download-token", exported)
        self.assertIn("HTTP 429", exported)
        with store.connection() as db:
            db.execute("DELETE FROM events WHERE id<=?", (identifiers[2],))
        response = self.client.get(f"/api/logs?after={before}")
        self.assertTrue(response.json["reset"])
        self.assertEqual([row["id"] for row in response.json["items"] if row["level"] == "ERROR"], identifiers[-2:])

    def test_cross_midnight_window_has_exclusive_end(self):
        config = self.app.extensions["ptflow_config"].get()
        config["schedule"].update(work_start="22:00", work_end="06:00")
        zone = ZoneInfo("Asia/Shanghai")
        for hour, expected in ((21, False), (22, True), (0, True), (5, True), (6, False)):
            self.assertEqual(is_work_time(config, datetime(2026, 11, 1, hour, tzinfo=zone)), expected)
        config["schedule"]["work_end"] = "22:00"
        self.assertTrue(is_work_time(config, datetime(2026, 11, 1, 12, tzinfo=zone)))

    def test_successful_sampling_does_not_evict_manual_runs_or_sampling_errors(self):
        self.setup_login()
        store = self.app.extensions["ptflow_store"]
        run = {"id": "manual-full", "kind": "full", "source": "manual", "started_at": 1,
               "finished_at": 2, "status": "succeeded", "message": "", "counts": {}}
        store.finish_run(run)
        store.finish_run({**run, "id": "sample-error", "kind": "collect", "source": "scheduled",
                          "started_at": 3, "finished_at": 4, "status": "failed"})
        for index in range(250):
            store.finish_run({**run, "id": f"sample-{index}", "kind": "collect", "source": "scheduled",
                              "started_at": 5 + index, "finished_at": 6 + index})
        history = self.client.get("/api/runs").json["items"]
        self.assertEqual([item["id"] for item in history], ["sample-error", "manual-full"])
        self.assertEqual(history[0]["status"], "failed")

    def test_native_gateway_requires_admin_setup_and_secures_https_session(self):
        with TemporaryDirectory() as data_dir:
            app = create_app(data_dir, gateway_prefix="/app/ptflow-fnos", start_scheduler=False)
            client = app.test_client()
            forwarded = {"X-Forwarded-Host": "nas.example", "X-Forwarded-Proto": "https"}
            response = client.get("/app/ptflow-fnos/api/auth", headers=forwarded)
            self.assertIn("Secure", response.headers["Set-Cookie"])
            self.assertIn("Path=/app/ptflow-fnos/", response.headers["Set-Cookie"])
            headers = {**forwarded, "Origin": "https://nas.example", "X-CSRF-Token": response.json["csrf_token"]}
            self.assertEqual(client.post("/app/ptflow-fnos/api/setup", json={"password": "native-long-password"}, headers=headers).status_code, 403)
            with redirect_stdout(StringIO()):
                response = client.post("/app/ptflow-fnos/api/setup", json={"password": "native-long-password"}, headers={**headers, "X-Trim-Isadmin": "true"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/app/ptflow-fnos/api/status", headers=forwarded).status_code, 200)


if __name__ == "__main__":
    unittest.main()
