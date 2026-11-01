"""Run locally over loopback or natively behind the fnOS Unix-socket gateway."""
from pathlib import Path
import argparse
import os
import signal

from waitress import create_server
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description="PTFlow — 飞牛原生刷流管理器")
    parser.add_argument("--data-dir", default=os.environ.get("PTFLOW_DATA_DIR") or os.environ.get("TRIM_PKGVAR") or str(Path.home() / ".local/share/ptflow"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--gateway-prefix", default=os.environ.get("GATEWAY_PREFIX", ""))
    arguments = parser.parse_args()
    os.umask(0o077)
    app = create_app(Path(arguments.data_dir).expanduser(), arguments.gateway_prefix)
    socket_path = os.environ.get("SOCKET_PATH", "")
    options = {"unix_socket": socket_path, "unix_socket_perms": "660"} if socket_path else {"host": arguments.host, "port": arguments.port}
    if socket_path:
        options.update(trusted_proxy="localhost", trusted_proxy_headers={"x-forwarded-proto", "x-forwarded-host", "x-forwarded-port"})
    server = create_server(app, threads=8, max_request_body_size=65536, **options)
    store = app.extensions["ptflow_store"]

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store.event("INFO", "系统", "Web 服务已就绪", details={"transport": "fnOS Unix socket" if socket_path else f"http://{arguments.host}:{arguments.port}", "data_dir": str(arguments.data_dir)})
    try:
        server.run()
    finally:
        app.extensions["ptflow_scheduler"].stop()
        server.close()
        server.task_dispatcher.shutdown(timeout=5)
        store.event("INFO", "系统", "PTFlow 已停止，配置、日志与种子确认记录已保留")


if __name__ == "__main__":
    main()
