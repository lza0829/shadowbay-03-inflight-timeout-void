# -*- coding: utf-8 -*-
"""真实 uvicorn HTTP 级：超时扫描 × 重连补投并发交错压一次。

断言：
* 失败闭：一令恰一个终态赢家，不出现「作废 + 已补」双发；
* 压测全程单连接同源快照 /同源校验 的在途数瞬时一致；
* 静息后 /总览、/对账、/同源校验 三数相等；
* 打戳口径下交错后仍全部补投送达。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def _造离线令(base: str, i: int, prefix: str):
    dev = f"{prefix}-{i}"
    with httpx.Client(base_url=base, timeout=30) as h:
        assert h.post("/设备册", json={"设备键": dev, "别名": dev}).status_code == 200
        assert h.post("/设备册/断线", json={"设备键": dev}).status_code == 200
        r = h.post("/指令廊/下发", json={"设备键": dev, "动作": "交错动作"})
        assert r.status_code == 200, r.text
        return dev, r.json()["令号"]


def test_http_作废口径_扫描与重连交错_失败闭_瞬时同源(live_base):
    base = live_base
    K = 8
    with httpx.Client(base_url=base, timeout=30) as h:
        h.post("/超时配置", json={"阈值秒": 0, "口径": "作废"})
    pairs = [_造离线令(base, i, "dev-http-v") for i in range(K)]

    采样 = []
    采样异常 = []
    stop = threading.Event()

    def 采样循环():
        with httpx.Client(base_url=base, timeout=30) as h:
            while not stop.is_set():
                try:
                    s = h.get("/同源校验").json()
                    o = h.get("/总览").json()
                    # 单快照内三数必须瞬时同源
                    assert s["在途数"] == s["总览在途指令"] == s["对账在途数"]
                    assert s["瞬时同源"] is True
                    assert o["在途指令"] == o["在途数"]
                    采样.append((s["在途数"], o["在途指令"], o["在途数"]))
                except Exception as e:  # noqa: BLE001
                    采样异常.append(repr(e))

    sampler = threading.Thread(target=采样循环, daemon=True)
    sampler.start()

    闸 = threading.Barrier(2 * K)

    def 扫描(no):
        闸.wait()
        with httpx.Client(base_url=base, timeout=30) as h:
            r = h.post("/超时扫描", json={"令号": no, "阈值秒": 0, "口径": "作废"})
            return r.status_code

    def 重连(dev):
        闸.wait()
        with httpx.Client(base_url=base, timeout=30) as h:
            r = h.post("/设备册/重连补投", json={"设备键": dev})
            return r.status_code

    with ThreadPoolExecutor(max_workers=2 * K) as ex:
        futs = []
        for dev, no in pairs:
            futs.append(ex.submit(扫描, no))
            futs.append(ex.submit(重连, dev))
        codes = [f.result(timeout=60) for f in futs]

    time.sleep(0.05)
    stop.set()
    sampler.join(timeout=5)

    assert codes and all(c == 200 for c in codes), codes
    assert not 采样异常, 采样异常
    assert len(采样) >= 1  # 压测期间确实采到了同源样本

    # 终态逐令查验
    with httpx.Client(base_url=base, timeout=30) as h:
        for dev, no in pairs:
            v = h.get(f"/超时查验?令号={no}").json()
            assert v["valid"] is True, v
            assert v["状态"] in ("作废", "送达")
            assert not (v["状态"] == "作废" and v["补投条状态"] == "已补"), "出现双发"
            if v["状态"] == "作废":
                assert v["补投条状态"] == "取消"
                again = h.post("/设备册/重连补投", json={"设备键": dev}).json()
                assert no not in again["令号"]
            else:
                assert v["补投条状态"] == "已补"

        # 静息后三端点在途数相等且为 0（本批），无幽灵
        o = h.get("/总览").json()
        r = h.get("/对账").json()
        s = h.get("/同源校验").json()
        assert o["在途指令"] == o["在途数"] == r["在途数"] == s["在途数"]
        assert r["幽灵补投条数"] == 0
        assert all(
            x["valid"] for x in r["行"]
            if x["令号"] in {no for _, no in pairs}
        )


def test_http_打戳口径_交错后仍补投送达(live_base):
    base = live_base
    K = 6
    with httpx.Client(base_url=base, timeout=30) as h:
        h.post("/超时配置", json={"阈值秒": 0, "口径": "打戳"})
    pairs = [_造离线令(base, i, "dev-http-s") for i in range(K)]

    闸 = threading.Barrier(3 * K)

    def 扫描(no):
        闸.wait()
        with httpx.Client(base_url=base, timeout=30) as h:
            return h.post("/超时扫描", json={"令号": no, "阈值秒": 0, "口径": "打戳"}).status_code

    def 重连(dev):
        闸.wait()
        time.sleep(0.01)
        with httpx.Client(base_url=base, timeout=30) as h:
            return h.post("/重连补投", json={"设备键": dev}).status_code

    with ThreadPoolExecutor(max_workers=3 * K) as ex:
        futs = []
        for dev, no in pairs:
            futs.append(ex.submit(扫描, no))
            futs.append(ex.submit(扫描, no))
            futs.append(ex.submit(重连, dev))
        assert all(f.result(timeout=60) == 200 for f in futs)

    with httpx.Client(base_url=base, timeout=30) as h:
        for dev, no in pairs:
            v = h.get(f"/超时查验?令号={no}").json()
            assert v["状态"] == "送达", v
            assert v["补投条状态"] == "已补"
            assert v["valid"] is True
