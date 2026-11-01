"""Build an offline fnOS FPK on a matching native Linux runner. No frontend toolchain."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parent.parent


def digest(path: Path, algorithm="sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def download(url: str, destination: Path, expected: str | None = None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".download")
        try:
            with urllib.request.urlopen(url, timeout=120) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
            if expected and digest(temporary) != expected:
                raise RuntimeError(f"Published SHA-256 verification failed: {url}")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    if expected and digest(destination) != expected:
        raise RuntimeError(f"Cached runtime digest mismatch; remove {destination}")


def write_tar(source: Path, destination: Path):
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

    def normalize(info):
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = epoch
        return info

    with destination.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
            archive.add(source, arcname=".", filter=normalize)


def point_segment_distance(x, y, x1, y1, x2, y2):
    fraction = max(0, min(1, ((x - x1) * (x2 - x1) + (y - y1) * (y2 - y1))
                          / ((x2 - x1) ** 2 + (y2 - y1) ** 2)))
    return math.hypot(x - x1 - fraction * (x2 - x1), y - y1 - fraction * (y2 - y1))


def icon_png(size: int) -> bytes:
    """Original P/flow badge, drawn and encoded with stdlib at build time only."""
    segments = [(30, 70, 30, 29), (30, 29, 51, 29), (51, 29, 59, 37),
                (59, 37, 59, 45), (59, 45, 51, 53), (51, 53, 30, 53),
                (45, 72, 61, 61), (61, 61, 72, 65), (72, 65, 81, 47),
                (81, 47, 70, 51), (81, 47, 83, 59)]
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            channels = [0, 0, 0, 0]
            for dy in (0.25, 0.75):
                for dx in (0.25, 0.75):
                    px, py = (x + dx) * 100 / size, (y + dy) * 100 / size
                    rounded = math.hypot(max(abs(px - 50) - 29, 0), max(abs(py - 50) - 29, 0)) <= 17
                    line = rounded and any(point_segment_distance(px, py, *segment) <= 3.5
                                           for segment in segments)
                    color = (235, 255, 249, 255) if line else (
                        int(13 + py * 0.08), int(157 - py * 0.28), int(132 - py * 0.16), 255)
                    if not rounded:
                        color = (0, 0, 0, 0)
                    for channel in range(4):
                        channels[channel] += color[channel]
            rows.extend(round(channel / 4) for channel in channels)

    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def build(arch: str, output: Path) -> Path:
    native = {"x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or native != arch:
        raise RuntimeError(f"{arch} requires a matching native Linux host; no emulation/cross-build")
    lock = json.loads((ROOT / "scripts/runtime.lock.json").read_text())
    asset = lock["assets"][arch]
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifest = (ROOT / "fpk/manifest").read_text()
    fields = dict(line.split("=", 1) for line in manifest.splitlines() if "=" in line)
    if {key.strip(): value.strip() for key, value in fields.items()}.get("version") != version:
        raise RuntimeError("pyproject.toml and fpk/manifest versions disagree")
    cache = ROOT / ".cache/runtime"
    runtime = cache / (arch + "-" + lock["release"] + ".tar.gz")
    download(asset["url"], runtime, asset["sha256"])
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ptflow-fpk-") as workspace:
        stage = Path(workspace) / "stage"
        package = Path(workspace) / "package"
        shutil.copytree(ROOT / "fpk", stage)
        app = stage / "app"
        with tarfile.open(runtime, "r:gz") as archive:
            archive.extractall(app, filter="data")
        python = app / "python/bin/python3"
        server = app / "server"
        shutil.copytree(ROOT / "ptflow", server / "ptflow",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
        subprocess.run([str(python), "-I", "-m", "pip", "install", "--no-compile", "--require-hashes",
                        "--only-binary=:all:", "-r", str(ROOT / "requirements.lock")],
                       env=environment, check=True)
        subprocess.run([str(python), "-I", "-c",
                        "import sys, flask, requests, waitress, sqlite3, ssl; "
                        "from zoneinfo import ZoneInfo; "
                        f"assert sys.version.split()[0] == {lock['python_version']!r}; "
                        "assert ZoneInfo('Asia/Shanghai') is not None"], env=environment, check=True)
        licenses = app / "licenses"
        licenses.mkdir(exist_ok=True)
        for name in ("LICENSE", "README.md", "CHANGELOG.md", "requirements.lock"):
            shutil.copy2(ROOT / name, licenses / name)
        python_licenses = licenses / "python-build-standalone"
        for name in lock["license_files"]:
            source = f"https://raw.githubusercontent.com/astral-sh/python-build-standalone/{lock['license_commit']}/{name}"
            cached = cache / "licenses" / lock["license_commit"] / name
            download(source, cached)
            python_licenses.mkdir(exist_ok=True)
            shutil.copy2(cached, python_licenses / name)
        shutil.copy2(ROOT / "scripts/runtime.lock.json", licenses / "runtime.lock.json")
        (licenses / "THIRD_PARTY_NOTICES.txt").write_text(
            f"PTFlow {version}: GPL-3.0-only; source https://github.com/mhduiy/ptflow-fnos\n"
            "Behavior reference: PTBrush https://github.com/mhduiy/ptbrush\n"
            "fnOS integration reference: Clash-for-fnos (GPL-3.0)\n"
            "https://github.com/chenpingonline/Clash-for-fnos\n"
            "Portable CPython: Python Software Foundation License; Python build tooling: MPL-2.0.\n"
            "See python-build-standalone/ for upstream runtime component license texts.\n"
            "Runtime provenance/digest: runtime.lock.json. Application dependencies: requirements.lock.\n"
            "Wheel metadata, copyright and license files remain bundled under python/lib/python3.12/site-packages/*.dist-info/.\n",
            encoding="utf-8")
        (app / "build-info.json").write_text(json.dumps({
            "version": version, "architecture": arch, "python": lock["python_version"],
            "runtime": asset, "requirements_sha256": digest(ROOT / "requirements.lock")}, indent=2) + "\n")
        for size in (64, 256):
            image = icon_png(size)
            (stage / ("ICON.PNG" if size == 64 else "ICON_256.PNG")).write_bytes(image)
            images = app / "ui/images"
            images.mkdir(exist_ok=True)
            (images / f"icon_{size}.png").write_bytes(image)
        package.mkdir()
        write_tar(app, package / "app.tgz")
        platform_name = "x86" if arch == "x86_64" else "arm"
        checksum = digest(package / "app.tgz", "md5")
        patched = []
        for line in manifest.splitlines():
            key = line.partition("=")[0].strip()
            if key == "platform":
                line = f"platform        = {platform_name}"
            elif key == "checksum":
                line = f"checksum        = {checksum}"
            patched.append(line)
        (package / "manifest").write_text("\n".join(patched) + "\n")
        for name in ("cmd", "config", "wizard"):
            shutil.copytree(stage / name, package / name)
            (package / name).chmod(0o755)
        for command in (package / "cmd").iterdir():
            command.chmod(0o755)
        for name in ("ICON.PNG", "ICON_256.PNG"):
            shutil.copy2(stage / name, package / name)
        result = output / f"PTFlow_{version}_{arch}.fpk"
        write_tar(package, result)
        result.with_suffix(".fpk.sha256").write_text(f"{digest(result)}  {result.name}\n")
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("architecture", choices=["x86_64", "arm64"])
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    print(build(arguments.architecture, arguments.output.resolve()))
