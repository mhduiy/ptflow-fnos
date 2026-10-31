"""Exercise the actual bundled service/lifecycle on an isolated root Linux CI runner."""
from http.cookies import SimpleCookie
from pathlib import Path
import argparse
import http.client
import json
import os
import pwd
import socket
import subprocess
import tarfile
import tempfile


def check_package(package):
    if os.geteuid() != 0:
        raise RuntimeError("Native lifecycle smoke requires root on the disposable Linux runner")
    username = "ptflow-fnos"
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", username], check=True)
        account = pwd.getpwnam(username)
    with tempfile.TemporaryDirectory(prefix="ptflow-native-smoke-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        outer, target, data = root / "package", root / "target", root / "var"
        outer.mkdir()
        target.mkdir()
        data.mkdir()
        with tarfile.open(package) as archive:
            archive.extractall(outer, filter="data")
        with tarfile.open(outer / "app.tgz") as archive:
            archive.extractall(target, filter="data")
        environment = dict(os.environ, TRIM_APPDEST=str(target), TRIM_PKGVAR=str(data),
                           TRIM_USERNAME=username, TRIM_GROUPNAME=username,
                           TRIM_TEMP_LOGFILE=str(root / "startup-error.txt"))
        command = str(outer / "cmd/main")
        cookie = ""
        csrf = ""

        def lifecycle(action, expected=0):
            result = subprocess.run([command, action], env=environment, text=True, capture_output=True, timeout=45)
            if result.returncode != expected:
                details = result.stdout + result.stderr
                for name in (root / "startup-error.txt", data / "ptflow.log"):
                    if name.exists():
                        details += name.read_text(errors="replace")
                raise AssertionError(f"{action} exited {result.returncode}, expected {expected}: {details}")

        def request(path, method="GET", body=None):
            nonlocal cookie
            connection = http.client.HTTPConnection("localhost", timeout=10)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(10)
            connection.sock.connect(str(data / "ptflow.sock"))
            headers = {"Host": "nas.local", "X-Forwarded-Host": "nas.local", "X-Forwarded-Proto": "https", "X-Trim-Isadmin": "true"}
            if cookie:
                headers["Cookie"] = cookie
            if body is not None:
                headers.update({"Content-Type": "application/json", "Origin": "https://nas.local", "X-CSRF-Token": csrf})
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if response.getheader("Set-Cookie"):
                parsed = SimpleCookie(response.getheader("Set-Cookie"))
                cookie = "; ".join(f"{key}={item.value}" for key, item in parsed.items())
            status = response.status
            connection.close()
            return status, raw

        try:
            lifecycle("status", 3)
            lifecycle("start")
            lifecycle("status")
            assert (data / "ptflow.sock").stat().st_mode & 0o777 == 0o660
            assert request("/health")[0] == 200
            assert request("/app/ptflow-fnos/")[0] == 200
            assert request("/app/ptflow-fnos/static/app.js")[0] == 200
            assert request("/app/ptflow-fnos/api/config")[0] == 401
            status, raw = request("/app/ptflow-fnos/api/auth")
            csrf = json.loads(raw)["csrf_token"]
            status, raw = request("/app/ptflow-fnos/api/setup", "POST", {"password": "native-ci-" + "private-check-123"})
            assert status == 200, raw
            csrf = json.loads(raw)["csrf_token"]
            status, raw = request("/app/ptflow-fnos/api/config", "PUT", {"schedule": {"query_minutes": 77}})
            assert status == 200 and json.loads(raw)["schedule"]["query_minutes"] == 77
            private = data / "data/config.json"
            assert private.stat().st_uid == account.pw_uid
            assert private.stat().st_mode & 0o777 == 0o600
            lifecycle("stop")
            lifecycle("status", 3)
            assert private.exists()
            assert not (data / "ptflow.sock").exists()
            lifecycle("start")
            lifecycle("status")
            status, raw = request("/app/ptflow-fnos/api/config")
            assert status == 200 and json.loads(raw)["schedule"]["query_minutes"] == 77
            print(f"PASS {package.name}: native non-root service, gateway assets, auth, socket permissions, graceful stop/restart and retained config")
        finally:
            lifecycle("stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+", type=Path)
    for package in parser.parse_args().packages:
        check_package(package.resolve())
