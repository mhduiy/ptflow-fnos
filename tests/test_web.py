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
        self.assertEqual(self.client.post("/api/setup", json={},
                                         headers={**self.origin, "X-CSRF-Token": "é"}).status_code, 403)
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

    def test_committed_writes_survive_failed_audit_write(self):
        old_secret, new_secret = "audit-old-managed-secret", "audit-new-managed-secret"
        with redirect_stdout(StringIO()):
            response = self.post("/api/setup", {"password": old_secret})
        self.assertEqual(response.status_code, 200)
        self.token = response.json["csrf_token"]
        store = self.app.extensions["ptflow_store"]
        with store.connection() as db:
            db.execute("CREATE TRIGGER audit_failure BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        response = self.client.put("/api/config", json={"schedule": {"query_minutes": 47}},
                                   headers={**self.origin, "X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.json["committed"])
        self.assertEqual(response.json["result"]["schedule"]["query_minutes"], 47)
        self.assertTrue(response.json["error"])
        self.assertEqual(Config(self.temporary.name).get()["schedule"]["query_minutes"], 47)
        response = self.post("/api/password", {"current_password": old_secret, "password": new_secret})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.json["committed"])
        self.assertTrue(response.json["result"]["authenticated"])
        self.token = response.json["result"]["csrf_token"]
        self.assertTrue(self.client.get("/api/auth").json["authenticated"])
        self.assertEqual(self.post("/api/logout", {}).status_code, 200)
        self.token = self.client.get("/api/auth").json["csrf_token"]
        self.assertEqual(self.post("/api/login", {"password": new_secret}).status_code, 200)

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

    def test_logs_reset_cursor_after_empty_or_older_database_restore(self):
        self.setup_login()
        store = self.app.extensions["ptflow_store"]
        response = self.client.get("/api/logs?after=9000").json
        self.assertTrue(response["reset"])
        self.assertEqual(response["reset_reason"], "rewound")
        self.assertEqual(response["cursor"], store.event_bounds()[1])
        with store.connection() as db:
            db.execute("DELETE FROM events")
        response = self.client.get("/api/logs?after=9000").json
        self.assertTrue(response["reset"])
        self.assertEqual(response["cursor"], 0)
        with redirect_stdout(StringIO()):
            identifier = store.event("INFO", "恢复", "restored event")
        response = self.client.get("/api/logs?after=0").json
        self.assertEqual([row["id"] for row in response["items"]], [identifier])

    def test_export_includes_entire_retained_history_independent_of_filters(self):
        self.setup_login()
        store = self.app.extensions["ptflow_store"]
        with redirect_stdout(StringIO()):
            for index in range(10097):
                store.event("INFO", "保留", f"retained-{index}")
        with store.connection() as db:
            retained = db.execute("SELECT message FROM events ORDER BY id").fetchall()
        self.assertEqual(len(retained), 10098)
        exported = self.client.get("/api/logs/export?level=ERROR&q=missing").get_data(as_text=True)
        self.assertEqual(len(exported.splitlines()), len(retained))
        self.assertIn(retained[0]["message"], exported)
        self.assertIn(retained[-1]["message"], exported)

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
            response = client.get("/app/ptflow-fnos?from=desktop", headers={"Host": "untrusted.example"})
            self.assertEqual(response.status_code, 308)
            self.assertEqual(response.headers["Location"], "/app/ptflow-fnos/?from=desktop")
            for path in (response.headers["Location"], "/app/ptflow-fnos/static/app.js", "/static/app.js"):
                with client.get(path) as resource:
                    self.assertEqual(resource.status_code, 200)
            self.assertEqual(client.get("/api/auth").status_code, 200)
            client = app.test_client()
            base_url = "https://nas.example:5667"
            response = client.get("/app/ptflow-fnos/api/auth", headers=forwarded, base_url=base_url)
            self.assertIn("Secure", response.headers["Set-Cookie"])
            self.assertIn("Path=/app/ptflow-fnos/", response.headers["Set-Cookie"])
            headers = {**forwarded, "Origin": base_url, "X-CSRF-Token": response.json["csrf_token"]}
            for origin in ("https://nas.example", "https://nas.example:5668", "http://nas.example:5667", "https://other.example:5667", "null"):
                with self.subTest(origin=origin):
                    rejected = client.post("/app/ptflow-fnos/api/setup", json={}, base_url=base_url,
                                           headers={**headers, "Origin": origin, "X-Trim-Isadmin": "true"})
                    self.assertEqual(rejected.status_code, 403)
            self.assertEqual(client.post("/app/ptflow-fnos/api/setup", json={"password": "gateway-admin-test-credential"}, headers=headers, base_url=base_url).status_code, 403)
            with redirect_stdout(StringIO()):
                response = client.post("/app/ptflow-fnos/api/setup", json={"password": "gateway-admin-test-credential"}, headers={**headers, "X-Trim-Isadmin": "true"}, base_url=base_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/app/ptflow-fnos/api/status", headers=forwarded, base_url=base_url).status_code, 200)

    def test_gateway_session_binds_full_browser_origin_when_proxy_omits_port(self):
        with TemporaryDirectory() as data_dir, redirect_stdout(StringIO()):
            app = create_app(data_dir, gateway_prefix="/app/ptflow-fnos", start_scheduler=False)
            client = app.test_client()
            base_url, browser_origin = "http://nas.example", "http://nas.example:5666"
            path = "/app/ptflow-fnos/api/"
            preflight = client.options(path + "auth", base_url=base_url, headers={"Origin": "http://other.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "X-PTFlow-Origin"})
            self.assertNotIn("Access-Control-Allow-Origin", preflight.headers)
            for invalid in ("http://other.example:5666", "https://nas.example:5666", "http://nas.example:5666/path", "http://user@nas.example:5666"):
                self.assertEqual(client.get(path + "auth", base_url=base_url, headers={"X-PTFlow-Origin": invalid}).status_code, 400)
            auth = client.get(path + "auth", base_url=base_url, headers={"X-PTFlow-Origin": browser_origin})
            headers = {"Origin": browser_origin, "X-CSRF-Token": auth.json["csrf_token"], "X-Trim-Isadmin": "true"}
            for invalid in ("http://nas.example", "http://nas.example:5667", "https://nas.example:5666", "http://other.example:5666", "null"):
                self.assertEqual(client.post(path + "setup", json={}, base_url=base_url, headers={**headers, "Origin": invalid}).status_code, 403)
            self.assertEqual(client.post(path + "setup", json={}, base_url=base_url, headers={**headers, "X-CSRF-Token": "wrong-token"}).status_code, 403)
            setup = client.post(path + "setup", json={"password": "native-gateway-browser-test-credential"}, base_url=base_url, headers=headers)
            self.assertEqual(setup.status_code, 200)
            self.assertTrue(client.get(path + "auth", base_url=base_url).json["authenticated"])
            headers["X-CSRF-Token"] = setup.json["csrf_token"]
            self.assertEqual(client.post(path + "logout", json={}, base_url=base_url, headers=headers).status_code, 200)
            self.assertFalse(client.get(path + "auth", base_url=base_url, headers={"X-PTFlow-Origin": browser_origin}).json["authenticated"])


if __name__ == "__main__":
    unittest.main()
