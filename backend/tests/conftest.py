# -*- coding: utf-8 -*-
"""pytest 夹具：在导入 app 之前把数据目录指到临时位置，保证用例隔离。"""
import os
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

_TMP = Path(tempfile.mkdtemp(prefix="shadowbay-test-"))
os.environ["数据目录"] = str(_TMP / "data")
os.environ.setdefault("预置目录", str(_BACKEND.parent / "fixtures"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.在途超时作废 import 确保结构  # noqa: E402

_ALL_TABLES = [
    "审计", "超时配置", "补投条", "指令廊", "凭证轮",
    "影子台", "空桩", "设备册",
]


def 清库():
    c = 连接()
    try:
        c.execute("PRAGMA foreign_keys=OFF")
        for t in _ALL_TABLES:
            c.execute(f"DELETE FROM {t}")
        try:
            c.execute("DELETE FROM sqlite_sequence")
        except Exception:
            pass
        c.commit()
        c.execute("PRAGMA foreign_keys=ON")
    finally:
        c.close()
    seed.初始化库()
    确保结构()


@pytest.fixture()
def client():
    清库()
    with TestClient(app) as cli:
        yield cli


@pytest.fixture()
def db():
    清库()
    c = 连接()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope="session")
def live_base():
    """起一个真实 uvicorn（同进程线程），供 HTTP 级并发交错测试。"""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    清库()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=port,
        log_level="warning", limit_concurrency=200,
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/健康", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.15)
    else:
        raise RuntimeError("uvicorn 测试服务未就绪")
    yield base
    server.should_exit = True
    time.sleep(0.2)
