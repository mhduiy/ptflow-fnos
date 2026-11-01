"""Exercise real FPK cmd consumers in a private Linux mount namespace, never a NAS."""
from http.cookies import SimpleCookie
from pathlib import Path
import argparse
import copy
import http.client
import json
import os
import pwd
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent


def check_package(package):
    # Reuse the existing loopback HTTP simulator, not fake lifecycle commands.
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    from test_engine import Simulator

    username = "ptflow-smoke-" + secrets.token_hex(4)
    subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", username], check=True)
    account = pwd.getpwnam(username)
    reader = username.replace("smoke", "reader")
    reader_created = False
    simulator = Simulator()
    try:
        subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", reader], check=True)
        reader_created = True
        with tempfile.TemporaryDirectory(prefix="ptflow-native-smoke-") as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            outer, target, data = root / "package", root / "target", root / "var"
            outer.mkdir()
            target.mkdir()
            data.mkdir()
            sentinel = root / "download-files" / "untouched.bin"
            sentinel.parent.mkdir()
            sentinel.write_bytes(b"download files must survive every lifecycle operation")
            sentinel_snapshot = sentinel.read_bytes(), sentinel.stat().st_uid, sentinel.stat().st_mode
            with tarfile.open(package) as archive:
                archive.extractall(outer, filter="data")

            def install_payload():
                with tarfile.open(outer / "app.tgz") as archive:
                    archive.extractall(target, filter="data")

            environment = dict(os.environ, TRIM_APPDEST=str(target), TRIM_PKGVAR=str(data),
                               TRIM_USERNAME=username, TRIM_GROUPNAME=username,
                               TRIM_TEMP_LOGFILE=str(root / "startup-error.txt"))
            record_path = Path("/run/ptflow-fnos/process.json")
            private = data / "data"
            cookie = csrf = ""
            password = "lifecycle-" + str(time.time_ns())
            cleanup_record = None

            def command(name, action=None, expected=0, extra=None):
                arguments = [str(outer / "cmd" / name)]
                if action is not None:
                    arguments.append(action)
                result = subprocess.run(arguments, env=dict(environment, **(extra or {})),
                                        text=True, capture_output=True, timeout=100)
                if result.returncode != expected:
                    details = result.stdout + result.stderr
                    for path in (root / "startup-error.txt", data / "ptflow.log"):
                        if path.is_file() and not path.is_symlink():
                            with path.open("rb") as log:
                                log.seek(max(0, path.stat().st_size - 8192))
                                details += log.read().decode(errors="replace")
                    raise AssertionError(f"{name} {action}: exit {result.returncode}, expected {expected}: {details}")
                return result

            def lifecycle(action, expected=0):
                nonlocal cleanup_record
                result = command("main", action, expected)
                if action == "start" and expected == 0:
                    cleanup_record = record_path.read_bytes()
                return result

            def request(path, method="GET", body=None, administrator=True):
                nonlocal cookie
                connection = http.client.HTTPConnection("localhost", timeout=10)
                try:
                    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    connection.sock.settimeout(10)
                    connection.sock.connect(str(data / "ptflow.sock"))
                    headers = {"Host": "internal.invalid", "X-Forwarded-Host": "nas.local",
                               "X-Forwarded-Proto": "https"}
                    if method == "GET" and path.endswith("/api/auth"):
                        headers["X-PTFlow-Origin"] = "https://nas.local:5667"
                    if administrator is not None:
                        headers["X-Trim-Isadmin"] = "true" if administrator else "false"
                    if cookie:
                        headers["Cookie"] = cookie
                    if body is not None:
                        headers.update({"Content-Type": "application/json", "Origin": "https://nas.local:5667",
                                        "X-CSRF-Token": csrf})
                    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
                    response = connection.getresponse()
                    raw = response.read()
                    if response.getheader("Set-Cookie"):
                        parsed = SimpleCookie(response.getheader("Set-Cookie"))
                        for item in parsed.values():
                            assert item["path"] == "/app/ptflow-fnos/" and item["secure"] and item["httponly"]
                        cookie = "; ".join(f"{key}={item.value}" for key, item in parsed.items())
                    return response.status, raw
                finally:
                    connection.close()

            def api(path, method="GET", body=None, administrator=True):
                status, raw = request("/app/ptflow-fnos/api/" + path, method, body, administrator)
                return status, json.loads(raw)

            def run(kind):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    status, state = api("status")
                    assert status == 200, state
                    collect = next(item for item in state["schedules"] if item["kind"] == "collect")
                    if state["current_run"] is None and state["sample"] and collect["next_at"] > state["now"]:
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError(f"Initial sampling did not finish: {state}")
                status, result = api("run", "POST", {"kind": kind})
                assert status == 202, result
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    status, history = api("runs")
                    item = next((item for item in history["items"] if item["id"] == result["id"]), None)
                    if item and item["status"] != "running" and api("status")[1]["current_run"] is None:
                        assert item["status"] == "succeeded", item
                        return item
                    time.sleep(0.1)
                raise AssertionError(f"{kind} did not finish: {result}")

            try:
                command("install_init")
                install_payload()
                command("main", expected=1)
                lifecycle("status", 3)
                command("install_callback")
                command("install_callback")
                command("config_init")
                command("config_callback")
                assert private.stat().st_mode & 0o777 == 0o700
                lifecycle("start")
                lifecycle("status")
                first_record = record_path.read_bytes()
                lifecycle("start")
                assert record_path.read_bytes() == first_record
                assert (data / "ptflow.sock").stat().st_mode & 0o777 == 0o660
                assert (data / "ptflow.sock").stat().st_uid == account.pw_uid
                assert (data / "ptflow.sock").stat().st_gid == account.pw_gid
                assert (target / "app.sock").is_symlink()
                assert os.readlink(target / "app.sock") == str(data / "ptflow.sock")
                for directory in (root, target, data):
                    assert directory.stat().st_mode & 0o111 == 0o111
                    assert not directory.stat().st_mode & 0o002
                probe = """
import http.client, os, socket, sys
assert os.geteuid() not in (0, int(sys.argv[2]))
allowed = sys.argv[4] == "allow"
groups = set(os.getgroups()) | {os.getgid()}
assert (int(sys.argv[3]) in groups) == allowed
connection = http.client.HTTPConnection("localhost", timeout=5)
try:
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(5)
    try:
        connection.sock.connect(sys.argv[1])
    except PermissionError:
        assert not allowed
        print("DENIED: ordinary UID without socket group")
    else:
        assert allowed
        connection.request("GET", "/health", headers={"Host": "localhost"})
        response = connection.getresponse()
        assert response.status == 200 and response.read()
        try:
            open(sys.argv[5], "rb")
        except PermissionError:
            pass
        else:
            raise AssertionError("Socket group must not expose private configuration")
        print("ALLOWED: different ordinary UID in socket group, private data still denied")
finally:
    connection.close()
"""
                for allowed in (True, False):
                    groups = ["-G", username] if allowed else []
                    result = subprocess.run(
                        ["runuser", "-u", reader, "-g", reader, *groups, "--",
                         str(target / "python/bin/python3"), "-I", "-c", probe,
                         str(target / "app.sock"), str(account.pw_uid), str(account.pw_gid),
                         "allow" if allowed else "deny", str(private / "config.json")],
                        text=True, capture_output=True, timeout=10)
                    assert result.returncode == 0, result.stdout + result.stderr
                    print(f"PASS {package.name}: {result.stdout.strip()}")
                for prefix in ("", "/app/ptflow-fnos"):
                    assert request(prefix + "/health")[0] == 200
                    assert request(prefix + "/")[0] == 200
                    assert request(prefix + "/static/app.js")[0] == 200
                    assert request(prefix + "/api/config")[0] == 401
                status, auth = api("auth")
                assert status == 200 and auth["setup_required"]
                csrf = auth["csrf_token"]
                for administrator in (None, False):
                    assert api("setup", "POST", {"password": password}, administrator)[0] == 403
                status, auth = api("setup", "POST", {"password": password})
                assert status == 200, auth
                csrf = auth["csrf_token"]
                assert api("setup", "POST", {"password": password})[0] == 409
                status, configuration = api("config", "PUT", {
                    "downloader": {"url": simulator.url, "username": "simulated-admin", "password": "simulated-credentials",
                                   "save_path": str(sentinel.parent)},
                    "mteam": {"url": simulator.url, "api_key": "smoke-site-secret", "modes": ["movie"],
                              "request_interval_seconds": 1},
                    "schedule": {"query_minutes": 77, "stats_seconds": 3600},
                    "strategy": {"min_free_gib": 1, "max_downloading": 1, "max_torrent_gib": 12 / 1024}})
                assert status == 200 and configuration["schedule"]["query_minutes"] == 77
                run("query")
                run("brush")
                status, candidates = api("candidates")
                submitted = [item for item in candidates["items"] if item["state"] == "submitted"]
                assert len(submitted) == 1 and submitted[0]["qb_hash"] in simulator.rows
                assert len(api("torrents")[1]["items"]) == 1
                for filename in ("config.json", "auth.json", "ptflow.sqlite3"):
                    info = (private / filename).stat()
                    assert info.st_uid == account.pw_uid and info.st_mode & 0o777 == 0o600

                # Corrupt records and reused PIDs never authorize a signal or a second live service.
                record_path.write_text("[]")
                lifecycle("status", 3)
                lifecycle("start", 1)
                lifecycle("stop", 1)
                assert request("/health")[0] == 200
                stale = json.loads(first_record)
                stale["service"]["start"] = str(int(stale["service"]["start"]) + 1)
                record_path.write_text(json.dumps(stale))
                lifecycle("stop", 1)
                assert request("/health")[0] == 200
                record_path.write_bytes(first_record)
                command("upgrade_init")
                command("upgrade_init")
                lifecycle("stop")
                lifecycle("status", 3)
                assert not (data / "ptflow.sock").exists() and not (target / "app.sock").is_symlink()
                saved = {name: (private / name).read_bytes() for name in ("config.json", "auth.json")}
                with sqlite3.connect(private / "ptflow.sqlite3") as database:
                    ownership = database.execute("SELECT id,qb_hash,state FROM candidates ORDER BY id").fetchall()
                shutil.rmtree(target)
                target.mkdir()
                install_payload()
                for name in ("config.json", "auth.json", "ptflow.sqlite3"):
                    os.chown(private / name, 0, 0)
                before_calls = list(simulator.calls)
                command("upgrade_callback")
                command("upgrade_callback")
                assert simulator.calls == before_calls
                assert all((private / name).read_bytes() == contents for name, contents in saved.items())
                assert all((private / name).stat().st_uid == account.pw_uid
                           for name in ("config.json", "auth.json", "ptflow.sqlite3"))
                lifecycle("start")
                assert api("config")[1]["schedule"]["query_minutes"] == 77
                assert len(api("torrents")[1]["items"]) == 1
                cookie = ""
                csrf = api("auth")[1]["csrf_token"]
                assert not api("auth")[1]["setup_required"]
                status, auth = api("login", "POST", {"password": password})
                assert status == 200, auth
                csrf = auth["csrf_token"]
                lifecycle("stop")
                with sqlite3.connect(private / "ptflow.sqlite3") as database:
                    assert database.execute("SELECT id,qb_hash,state FROM candidates ORDER BY id").fetchall() == ownership

                for value in ("{", "[]", "null", '{"service":{}}'):
                    record_path.write_text(value)
                    lifecycle("status", 3)
                    lifecycle("stop")
                    assert not record_path.exists()
                foreign = subprocess.Popen(["sleep", "60"])
                try:
                    forged = copy.deepcopy(json.loads(first_record))
                    forged["service"]["pid"] = foreign.pid
                    record_path.write_text(json.dumps(forged))
                    lifecycle("stop")
                    assert foreign.poll() is None
                    forged["data"] = str(sentinel.parent)
                    record_path.write_text(json.dumps(forged))
                    lifecycle("stop", 1)
                    assert foreign.poll() is None
                    record_path.unlink()
                finally:
                    foreign.terminate()
                    foreign.wait(timeout=5)
                record_path.symlink_to(sentinel)
                lifecycle("start", 1)
                assert record_path.is_symlink()
                record_path.unlink()
                for path in (target / "app.sock", data / "ptflow.sock", data / "ptflow.log"):
                    if path.exists():
                        path.unlink()
                    path.symlink_to(sentinel)
                    lifecycle("start", 1)
                    assert path.is_symlink()
                    path.unlink()
                moved = data / "saved-data"
                private.rename(moved)
                private.symlink_to(sentinel.parent, target_is_directory=True)
                lifecycle("start", 1)
                command("uninstall_init", expected=1, extra={"wizard_delete_data": "true"})
                private.unlink()
                moved.rename(private)
                for path in (target / "app.sock", data / "ptflow.sock"):
                    path.write_text("not a socket")
                    lifecycle("start", 1)
                    assert path.read_text() == "not a socket"
                    path.unlink()
                (target / "app.sock").symlink_to(data / "ptflow.sock")
                (data / "ptflow.log").write_bytes(b"old" + b"x" * (1024 * 1024))
                archive = data / "ptflow.log.old"
                archive.symlink_to(sentinel)
                lifecycle("start", 1)
                archive.unlink()
                lifecycle("start")
                assert archive.read_bytes() == b"x" * (1024 * 1024)
                assert archive.stat().st_mode & 0o777 == 0o600
                lifecycle("stop")
                # Break only the isolated payload; a real failing process must leave no runtime debris.
                module = target / "server/ptflow/__main__.py"
                module.rename(module.with_suffix(".disabled"))
                lifecycle("start", 1)
                lifecycle("status", 3)
                assert not record_path.exists() and not (target / "app.sock").is_symlink()
                assert not (data / "ptflow.sock").exists()
                assert (root / "startup-error.txt").read_text().startswith("PTFlow lifecycle:")
                module.with_suffix(".disabled").rename(module)

                before_calls = list(simulator.calls)
                qb_rows = copy.deepcopy(simulator.rows)
                command("uninstall_init")
                command("uninstall_init", extra={"wizard_delete_data": "false"})
                command("uninstall_init", extra={"wizard_delete_data": "TRUE"})
                assert all((private / name).read_bytes() == contents for name, contents in saved.items())
                assert (private / "ptflow.sqlite3").exists() and (data / "ptflow.log").exists() and archive.exists()
                command("uninstall_callback", extra={"TRIM_APPDEST": str(root / "removed-target")})
                command("install_callback")
                assert simulator.calls == before_calls
                lifecycle("start")
                assert api("auth")[1]["authenticated"] and len(api("torrents")[1]["items"]) == 1
                lifecycle("stop")
                before_calls = list(simulator.calls)
                unrelated = data / "unrelated.bin"
                unrelated.write_bytes(b"not PTFlow application data")
                command("uninstall_init", extra={"wizard_delete_data": "true"})
                command("uninstall_init", extra={"wizard_delete_data": "true"})
                assert not private.exists() and not (data / "ptflow.log").exists() and not archive.exists()
                command("uninstall_callback", extra={"wizard_delete_data": "true",
                                                     "TRIM_APPDEST": str(root / "removed-target")})
                assert unrelated.read_bytes() == b"not PTFlow application data"
                assert simulator.calls == before_calls and simulator.rows == qb_rows
                assert (sentinel.read_bytes(), sentinel.stat().st_uid, sentinel.stat().st_mode) == sentinel_snapshot
                print(f"PASS {package.name}: real cmd install/repeated start-stop/upgrade/reinstall; both gateway paths, administrator setup, retained credentials/SQLite ownership; private permissions, bounded log, stale/corrupt/foreign PID safety, symlink refusal, failed-start cleanup; explicit app-only deletion and no uninstall qB calls (loopback simulator, not fnOS manager)")
            finally:
                if record_path.is_symlink():
                    record_path.unlink()
                if cleanup_record is not None:
                    record_path.write_bytes(cleanup_record)
                for path in (target / "app.sock", data / "ptflow.sock"):
                    if path.is_symlink():
                        path.unlink()
                lifecycle("stop")
    finally:
        simulator.close()
        subprocess.run(["userdel", username], check=True)
        if reader_created:
            subprocess.run(["userdel", reader], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("packages", nargs="+", type=Path)
    arguments = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Native lifecycle smoke requires root on a disposable native Linux runner")
    if not arguments.isolated_run:
        raise SystemExit(subprocess.run(["unshare", "--mount", "--fork", "--propagation", "private",
                                         sys.executable, str(Path(__file__).resolve()), "--isolated-run",
                                         *(str(package.resolve()) for package in arguments.packages)]).returncode)
    subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=755", "tmpfs", "/run"], check=True)
    for package in arguments.packages:
        check_package(package.resolve())
