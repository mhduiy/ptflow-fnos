"""Root-only fnOS lifecycle; the HTTP application always runs as the package user."""
from __future__ import annotations

import fcntl
import grp
import http.client
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time


STATE_DIR = Path("/run/ptflow-fnos")


def process_identity(pid: int) -> dict | None:
    """Include Linux start ticks so a reused PID can never become a stop target."""
    if pid <= 1:
        return None
    try:
        proc = Path(f"/proc/{pid}")
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return None
        uids = next(line.split()[1:] for line in (proc / "status").read_text().splitlines()
                    if line.startswith("Uid:"))
        return {"pid": pid, "start": fields[19], "uids": uids,
                "exe": os.readlink(proc / "exe"),
                "argv": (proc / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")}
    except (OSError, ValueError, IndexError, StopIteration, UnicodeError):
        return None


def signal_verified(identity: dict, sig: int) -> bool:
    """Open a pidfd before comparing identity; signaling then cannot race PID reuse."""
    try:
        fd = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return False
    try:
        if process_identity(identity["pid"]) != identity:
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def health(socket_path: Path) -> bool:
    connection = http.client.HTTPConnection("localhost", timeout=1)
    try:
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(1)
        connection.sock.connect(str(socket_path))
        connection.request("GET", "/health", headers={"Host": "localhost"})
        response = connection.getresponse()
        return response.status == 200 and len(response.read(4096)) > 0
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def rotate_startup_log(path: Path, limit: int = 1024 * 1024):
    if path.is_symlink():
        raise RuntimeError("Startup log must not be a symlink")
    if path.stat().st_size < limit:
        return
    previous = path.with_suffix(".log.old")
    if previous.is_symlink():
        raise RuntimeError("Archived startup log must not be a symlink")
    with path.open("rb+") as current:
        current.seek(-limit, os.SEEK_END)
        tail = current.read(limit)
        with previous.open("wb") as archive:
            archive.write(tail)
        previous.chmod(0o600)
        current.seek(0)
        current.truncate()


class Lifecycle:
    def __init__(self):
        if os.geteuid() != 0:
            raise RuntimeError("fnOS lifecycle must run as root; the web service must not")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("Linux pidfd support is required for safe process management")
        self.app = self.native_path("TRIM_APPDEST")
        self.pkgvar = self.native_path("TRIM_PKGVAR")
        if self.app == self.pkgvar or self.app in self.pkgvar.parents or self.pkgvar in self.app.parents:
            raise RuntimeError("Application and persistent data paths must be separate")
        self.data = self.pkgvar / "data"
        self.socket = self.pkgvar / "ptflow.sock"
        self.link = self.app / "app.sock"
        self.log = self.pkgvar / "ptflow.log"
        self.python = str((self.app / "python/bin/python3").resolve(strict=True))
        self.user = pwd.getpwnam(os.environ.get("TRIM_USERNAME", "ptflow-fnos"))
        self.group = grp.getgrnam(os.environ.get("TRIM_GROUPNAME", self.user.pw_name))
        if self.user.pw_uid == 0 or self.group.gr_gid == 0:
            raise RuntimeError("PTFlow requires a dedicated non-root user and group")
        self.command = [str(self.app / "python/bin/python3"), "-P", "-m", "ptflow",
                        "--data-dir", str(self.data)]
        self.record_path = STATE_DIR / "process.json"

    @staticmethod
    def native_path(name: str) -> Path:
        raw = os.environ.get(name, "")
        if not raw or not Path(raw).is_absolute():
            raise RuntimeError(f"Missing or non-absolute {name}")
        path = Path(raw).resolve()
        if len(path.parts) < 4 or path in {Path("/usr/local"), Path("/var/lib"), Path("/var/run")}:
            raise RuntimeError(f"Unsafe {name}: {path}")
        return path

    def prepare(self):
        self.pkgvar.mkdir(parents=True, exist_ok=True)
        os.chown(self.pkgvar, self.user.pw_uid, self.group.gr_gid)
        # The gateway needs traversal to the socket; secrets stay in data/ (0700).
        self.pkgvar.chmod(0o755)
        if self.data.is_symlink():
            raise RuntimeError("Persistent data directory must not be a symlink")
        self.data.mkdir(mode=0o700, exist_ok=True)
        for _, directories, files, directory_fd in os.fwalk(self.data, follow_symlinks=False):
            os.fchown(directory_fd, self.user.pw_uid, self.group.gr_gid)
            for name in directories + files:
                os.chown(name, self.user.pw_uid, self.group.gr_gid,
                         dir_fd=directory_fd, follow_symlinks=False)
        self.data.chmod(0o700)
        if self.log.is_symlink():
            raise RuntimeError("Lifecycle log must not be a symlink")
        self.log.touch(mode=0o600, exist_ok=True)
        os.chown(self.log, self.user.pw_uid, self.group.gr_gid)
        self.log.chmod(0o600)

    def read_record(self) -> dict | None:
        if self.record_path.is_symlink():
            raise RuntimeError("Process record must not be a symlink")
        try:
            record = json.loads(self.record_path.read_text())
        except (FileNotFoundError, ValueError, UnicodeError):
            return None
        if not isinstance(record, dict) or not isinstance(record.get("service"), dict):
            return None
        service = record["service"]
        if (not isinstance(record.get("data"), str)
                or type(service.get("pid")) is not int or service["pid"] <= 1
                or not isinstance(service.get("start"), str) or not service["start"].isascii()
                or not service["start"].isdecimal()
                or not isinstance(service.get("exe"), str)
                or not isinstance(service.get("argv"), list)
                or not all(isinstance(item, str) for item in service["argv"])
                or not isinstance(service.get("uids"), list)
                or len(service["uids"]) != 4
                or not all(isinstance(item, str) and item.isascii() and item.isdecimal()
                           for item in service["uids"])):
            return None
        if (record.get("data") != str(self.data) or service.get("exe") != self.python
                or service.get("argv") != self.command
                or service.get("uids") != [str(self.user.pw_uid)] * 4):
            raise RuntimeError("Process record belongs to another application; refusing to signal it")
        return record

    def ready(self, record: dict | None) -> bool:
        return bool(record and process_identity(record["service"]["pid"]) == record["service"]
                    and self.link.is_symlink() and os.readlink(self.link) == str(self.socket)
                    and health(self.socket))

    def clean_socket(self):
        if self.link.is_symlink():
            if os.readlink(self.link) != str(self.socket):
                raise RuntimeError("Gateway socket link has an unexpected target")
            self.link.unlink()
        elif self.link.exists():
            raise RuntimeError("Gateway socket path is not a symlink")
        if self.socket.exists() or self.socket.is_symlink():
            if not stat.S_ISSOCK(self.socket.lstat().st_mode):
                raise RuntimeError("Runtime socket path is not a Unix socket")
            self.socket.unlink()

    def stop(self):
        record = self.read_record()
        if record and signal_verified(record["service"], signal.SIGTERM):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process_identity(record["service"]["pid"]) != record["service"]:
                    break
                time.sleep(0.2)
            else:
                signal_verified(record["service"], signal.SIGKILL)
                print("PTFlow exceeded the 30-second graceful shutdown deadline", file=sys.stderr)
                deadline = time.monotonic() + 5
                while (time.monotonic() < deadline
                       and process_identity(record["service"]["pid"]) == record["service"]):
                    time.sleep(0.1)
        if record and process_identity(record["service"]["pid"]) == record["service"]:
            raise RuntimeError("PTFlow process has not stopped; preserving socket and PID record")
        if health(self.socket):
            raise RuntimeError("An untracked live service owns the socket; refusing to replace it")
        self.record_path.unlink(missing_ok=True)
        self.clean_socket()

    def start(self):
        record = self.read_record()
        if self.ready(record):
            return
        self.stop()
        self.prepare()
        rotate_startup_log(self.log)
        self.link.symlink_to(self.socket)
        environment = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(self.data),
            "USER": self.user.pw_name, "LOGNAME": self.user.pw_name,
            "LANG": "C.UTF-8", "PYTHONPATH": str(self.app / "server"),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "PTFLOW_DATA_DIR": str(self.data), "TRIM_PKGVAR": str(self.pkgvar),
            "SOCKET_PATH": str(self.socket), "GATEWAY_PREFIX": "/app/ptflow-fnos",
        }
        launcher = None
        service = None
        launcher_identity = None
        try:
            with self.log.open("ab", buffering=0) as output:
                launcher = subprocess.Popen(
                    ["/usr/sbin/runuser", "-u", self.user.pw_name, "--", *self.command],
                    cwd=self.app / "server", env=environment, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
            launcher_identity = process_identity(launcher.pid)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if launcher.poll() is not None:
                    raise RuntimeError(f"PTFlow exited during startup (runuser={launcher.returncode})")
                if not service:
                    children = Path(f"/proc/{launcher.pid}/task/{launcher.pid}/children").read_text().split()
                    for pid in children:
                        candidate = process_identity(int(pid))
                        if (candidate and candidate["exe"] == self.python
                                and candidate["argv"] == self.command
                                and candidate["uids"] == [str(self.user.pw_uid)] * 4):
                            service = candidate
                            record = {"data": str(self.data), "service": service}
                            temporary = self.record_path.with_suffix(".tmp")
                            temporary.write_text(json.dumps(record))
                            temporary.chmod(0o600)
                            temporary.replace(self.record_path)
                            break
                if service:
                    if process_identity(service["pid"]) != service:
                        raise RuntimeError("PTFlow exited before health readiness")
                    if self.socket.exists() and stat.S_ISSOCK(self.socket.lstat().st_mode):
                        self.socket.chmod(0o660, follow_symlinks=False)
                        if self.ready(record):
                            return
                time.sleep(0.2)
            raise RuntimeError("PTFlow /health readiness timed out after 30 seconds")
        except BaseException:
            if service:
                self.stop()
            elif launcher_identity:
                signal_verified(launcher_identity, signal.SIGTERM)
                if launcher is not None:
                    try:
                        launcher.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        signal_verified(launcher_identity, signal.SIGKILL)
                        launcher.wait(timeout=5)
            self.clean_socket()
            raise

    def uninstall(self):
        self.stop()
        if os.environ.get("wizard_delete_data", "false") == "true":
            if self.data.is_symlink():
                raise RuntimeError("Refusing to delete a symlinked data directory")
            if self.data.exists():
                shutil.rmtree(self.data)
            self.log.unlink(missing_ok=True)
            self.log.with_suffix(".log.old").unlink(missing_ok=True)
        # fnOS retains TRIM_PKGVAR; no configuration is stored in replaceable app/.


def main() -> int:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action not in {"start", "stop", "status", "prepare", "uninstall"}:
        print("Usage: lifecycle.py start|stop|status|prepare|uninstall", file=sys.stderr)
        return 1
    try:
        lifecycle = Lifecycle()
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        info = STATE_DIR.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise RuntimeError("Unsafe lifecycle state directory")
        STATE_DIR.chmod(0o700)
        fd = os.open(STATE_DIR / "lifecycle.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != 0:
                raise RuntimeError("Unsafe lifecycle lock owner")
            deadline = time.monotonic() + 90
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Lifecycle lock timed out after 90 seconds")
                    time.sleep(0.2)
            if action == "status":
                return 0 if lifecycle.ready(lifecycle.read_record()) else 3
            getattr(lifecycle, action)()
            return 0
        finally:
            os.close(fd)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        message = f"PTFlow lifecycle: {error}"
        print(message, file=sys.stderr)
        if os.environ.get("TRIM_TEMP_LOGFILE"):
            Path(os.environ["TRIM_TEMP_LOGFILE"]).write_text(message + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
