"""Runnable packaging checks: real Unix HTTP, live PID identity, and produced tar/PNG bytes."""
import copy
import importlib.util
import io
import os
from pathlib import Path
import signal
import socketserver
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from types import SimpleNamespace
import zlib

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


build = load("package_build", ROOT / "scripts/build.py")
lifecycle = load("native_lifecycle", ROOT / "fpk/app/lifecycle.py")


class PackagingTests(unittest.TestCase):
    def test_startup_log_rotation_retains_only_bounded_latest_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ptflow.log"
            path.write_bytes(b"old-content-latest")
            lifecycle.rotate_startup_log(path, limit=6)
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual(path.with_suffix(".log.old").read_bytes(), b"latest")
            path.write_bytes(b"new")
            lifecycle.rotate_startup_log(path, limit=6)
            self.assertEqual(path.read_bytes(), b"new")
            self.assertEqual(path.with_suffix(".log.old").read_bytes(), b"latest")

    def test_corrupt_record_stop_recovers_stale_gateway_without_a_signal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = object.__new__(lifecycle.Lifecycle)
            manager.data = root / "data"
            manager.socket = root / "ptflow.sock"
            manager.link = root / "app.sock"
            manager.record_path = root / "process.json"
            manager.python = "/isolated/python3"
            manager.command = [manager.python, "-m", "ptflow"]
            manager.user = SimpleNamespace(pw_uid=os.getuid())
            for damaged in (b"{", b"[]", b"null", b'{"service":{}}', b"\xff"):
                with self.subTest(damaged=damaged):
                    manager.record_path.write_bytes(damaged)
                    manager.link.symlink_to(manager.socket)
                    self.assertIsNone(manager.read_record())
                    manager.stop()
                    self.assertFalse(manager.record_path.exists())
                    self.assertFalse(manager.link.is_symlink())

    def test_startup_log_symlinks_never_modify_external_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external = root / "download.bin"
            external.write_bytes(b"download data")
            path = root / "ptflow.log"
            path.symlink_to(external)
            with self.assertRaises(RuntimeError):
                lifecycle.rotate_startup_log(path, limit=6)
            path.unlink()
            path.write_bytes(b"oversized log")
            path.with_suffix(".log.old").symlink_to(external)
            with self.assertRaises(RuntimeError):
                lifecycle.rotate_startup_log(path, limit=6)
            self.assertEqual(external.read_bytes(), b"download data")
            self.assertEqual(path.read_bytes(), b"oversized log")

    def test_fpk_manifest_is_addressable_at_package_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            package.mkdir()
            manifest = b"appname = ptflow-fnos\n"
            (package / "manifest").write_bytes(manifest)
            archive_path = root / "probe.fpk"
            build.write_tar(package, archive_path)
            with tarfile.open(archive_path, "r:gz") as archive:
                self.assertEqual(archive.extractfile("manifest").read(), manifest)

    def test_generated_original_png_and_nested_app_archive(self):
        image = build.icon_png(64)
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack("!II", image[16:24]), (64, 64))
        position, compressed = 8, bytearray()
        while position < len(image):
            length = struct.unpack("!I", image[position:position + 4])[0]
            kind = image[position + 4:position + 8]
            data = image[position + 8:position + 8 + length]
            crc = struct.unpack("!I", image[position + 8 + length:position + 12 + length])[0]
            self.assertEqual(crc, zlib.crc32(kind + data))
            if kind == b"IDAT":
                compressed.extend(data)
            position += 12 + length
        self.assertEqual(len(zlib.decompress(compressed)), 64 * (1 + 64 * 4))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            (app / "ui").mkdir(parents=True)
            (app / "ui/icon.png").write_bytes(image)
            first, second = root / "app.tgz", root / "second.tgz"
            build.write_tar(app, first)
            build.write_tar(app, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(fileobj=io.BytesIO(first.read_bytes()), mode="r:gz") as archive:
                restored = root / "restored"
                archive.extractall(restored, filter="data")
                self.assertEqual((restored / "ui/icon.png").read_bytes(), image)

    @unittest.skipUnless(hasattr(socketserver, "UnixStreamServer"), "Unix socket platform required")
    def test_readiness_requires_real_successful_http_response(self):
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = self.rfile.readline()
                if not request.startswith(b"GET /health HTTP/"):
                    return
                while self.rfile.readline() not in (b"\r\n", b""):
                    continue
                code = self.server.response_code
                self.wfile.write(f"HTTP/1.1 {code} status\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}".encode())

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service.sock"
            with socketserver.UnixStreamServer(str(path), Handler) as server:
                server.response_code = 503
                thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
                thread.start()
                try:
                    self.assertFalse(lifecycle.health(path))
                    server.response_code = 200
                    self.assertTrue(lifecycle.health(path))
                finally:
                    server.shutdown()
                    thread.join()
            self.assertFalse(lifecycle.health(path))

    @unittest.skipUnless(sys.platform == "linux" and hasattr(os, "pidfd_open"), "Linux pidfd required")
    def test_pid_identity_mismatch_never_signals_a_live_foreign_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            identity = lifecycle.process_identity(process.pid)
            self.assertIsNotNone(identity)
            foreign = copy.deepcopy(identity)
            foreign["start"] = str(int(foreign["start"]) + 1)
            self.assertFalse(lifecycle.signal_verified(foreign, signal.SIGTERM))
            self.assertIsNone(process.poll())
            self.assertTrue(lifecycle.signal_verified(identity, signal.SIGTERM))
            process.wait(timeout=5)
            self.assertFalse(lifecycle.signal_verified(identity, signal.SIGTERM))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
