"""Inspect actual FPK artifacts without executing or extracting their contents."""
import hashlib
import io
from pathlib import Path
import struct
import sys
import tarfile


def audit(path: Path):
    checksum_path = path.with_suffix(".fpk.sha256")
    expected, name = checksum_path.read_text().strip().split(maxsplit=1)
    assert name == path.name, "SHA-256 filename mismatch"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, "Outer SHA-256 mismatch"
    with tarfile.open(path, "r:gz") as package:
        files = {member.name: member for member in package.getmembers()}
        for name in ("app.tgz", "manifest", "ICON.PNG", "ICON_256.PNG", "cmd/main",
                     "config/privilege", "config/resource", "wizard/uninstall"):
            assert name in files, f"Missing outer member {name}"
        manifest = package.extractfile(files["manifest"]).read().decode()
        fields = dict(line.split("=", 1) for line in manifest.splitlines() if "=" in line)
        fields = {key.strip(): value.strip() for key, value in fields.items()}
        assert fields["appname"] == "ptflow-fnos"
        assert fields["os_min_version"] == "1.1.3100"
        assert fields["platform"] in {"x86", "arm"}
        payload = package.extractfile(files["app.tgz"]).read()
        assert hashlib.md5(payload).hexdigest() == fields["checksum"], "app.tgz MD5 mismatch"
        for name, size in (("ICON.PNG", 64), ("ICON_256.PNG", 256)):
            image = package.extractfile(files[name]).read()
            assert image[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack("!II", image[16:24]) == (size, size)
        assert files["cmd/main"].mode & 0o111, "Lifecycle entry is not executable"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as app:
        files = {member.name: member for member in app.getmembers()}
        for name in ("python/bin/python3.12", "server/ptflow/__main__.py", "server/ptflow/web.py",
                     "server/ptflow/static/index.html", "server/ptflow/static/app.css",
                     "server/ptflow/static/app.js", "ui/config", "lifecycle.py", "licenses/LICENSE",
                     "licenses/runtime.lock.json", "build-info.json"):
            assert name in files, f"Missing offline app member {name}"
        executable = app.extractfile(files["python/bin/python3.12"]).read(20)
        assert executable[:4] == b"\x7fELF", "Runtime is not Linux ELF"
        machine = struct.unpack("<H", executable[18:20])[0]
        assert machine == (62 if fields["platform"] == "x86" else 183), "Runtime architecture mismatch"
        for dependency in ("flask", "requests", "waitress", "certifi", "tzdata"):
            assert any(name.startswith(f"python/lib/python3.12/site-packages/{dependency}/")
                       for name in files), f"Missing offline dependency {dependency}"
    print(f"{path.name}: outer SHA-256, nested MD5, native ELF, gateway, icons and offline dependencies OK")


if __name__ == "__main__":
    paths = [Path(argument) for argument in sys.argv[1:]] or sorted(Path("dist").glob("*.fpk"))
    if not paths:
        raise SystemExit("No FPK artifacts supplied")
    for artifact in paths:
        audit(artifact)
