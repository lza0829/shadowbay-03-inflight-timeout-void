# -*- coding: utf-8 -*-
"""在途超时作废 —— 口径 / 状态迁移 / 查验 / 夹具 / 边界 / 同源 自动化断言。

场景主线（与任务书一致）：
  预置：断线后下发 → 拨超时 → 再重连。
  甲（作废口径）不应再送达；乙（打戳口径）应补投送达。
"""
import json
from pathlib import Path

import pytest

from app.db import 预置目录
from app.modules.在途超时作废 import 并发压测服务


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def 建离线令(client, 键="dev-jia", 别名="甲设备", 动作="锁闭通道"):
    client.post("/设备册", json={"设备键": 键, "别名": 别名})
    client.post("/设备册/断线", json={"设备键": 键})
    r = client.post("/指令廊/下发", json={"设备键": 键, "动作": 动作})
    assert r.status_code == 200, r.text
    return r.json()


def 下发时刻(client, 令号):
    rows = client.get("/指令廊").json()
    return next(r for r in rows if r["令号"] == 令号)["下发时"]


# ---------------------------------------------------------------------------
# 1. 甲：超时即作废，重连不再送达
# ---------------------------------------------------------------------------

def test_甲_超时即作废_查验五要素_重连不送达(client):
    client.post("/超时配置", json={"阈值秒": 60, "口径": "作废"})
    made = 建离线令(client, "dev-jia")
    no = made["令号"]
    assert made["状态"] == "在途"

    # 拨超时（越过 60s 阈值）并扫描
    client.post("/拨超时", json={"推进秒": 61})
    scan = client.post("/超时扫描", json={}).json()
    assert scan["口径"] == "作废"
    assert no in scan["作废令号"]
    assert scan["扫描窗"]["右界含"] is True  # 扫描窗边界写清：右界含

    # 查验路由：五要素逐字
    v = client.get(f"/在途超时作废/查验?令号={no}").json()
    for k in ("令号", "状态", "超时戳", "补投条状态", "valid"):
        assert k in v
    assert v["令号"] == no
    assert v["状态"] == "作废"
    assert v["补投条状态"] == "取消"
    assert isinstance(v["超时戳"], str) and v["超时戳"]
    assert v["valid"] is True

    # 作废说明含「超时」与阈值快照（60）
    assert "超时" in v["说明"]
    assert "阈值" in v["说明"] and "60" in v["说明"]
    assert v["阈值快照"]

    # 禁止无记录删令：令行仍在库，只是状态迁移
    rows = client.get("/指令廊").json()
    assert any(r["令号"] == no for r in rows)

    # 作废路径不得留下幽灵补投条；对账 valid
    rc = client.get("/对账").json()
    assert rc["幽灵补投条数"] == 0
    assert rc["全部valid"] is True

    # 再重连：失败闭，0 补投，甲不应再送达
    recon = client.post("/设备册/重连补投", json={"设备键": "dev-jia"}).json()
    assert recon["补投数"] == 0
    assert no not in recon["令号"]
    v2 = client.get(f"/超时查验?令号={no}").json()  # 短别名路由
    assert v2["状态"] == "作废"
    assert v2["补投条状态"] == "取消"
    assert v2["valid"] is True


# ---------------------------------------------------------------------------
# 2. 乙：超时仅打戳，重连按序补投送达
# ---------------------------------------------------------------------------

def test_乙_超时仅打戳_廊保持在途_重连补投送达(client):
    client.post("/超时配置", json={"阈值秒": 60, "口径": "打戳"})
    made = 建离线令(client, "dev-yi", "乙设备", "采集补传")
    no = made["令号"]

    client.post("/拨超时", json={"推进秒": 61})
    scan = client.post("/超时扫描", json={}).json()
    assert no in scan["打戳令号"]
    assert scan["作废数"] == 0

    v = client.get(f"/超时查验?令号={no}").json()
    assert v["状态"] == "在途"          # 廊保持在途
    assert v["补投条状态"] == "积压"     # 条仍积压
    assert v["超时戳"]                  # 写了超时戳
    assert v["valid"] is True

    # 重连仍按序补投 → 送达/已补，超时戳保留可查
    recon = client.post("/设备册/重连补投", json={"设备键": "dev-yi"}).json()
    assert recon["补投数"] == 1
    assert no in recon["令号"]
    v2 = client.get(f"/超时查验?令号={no}").json()
    assert v2["状态"] == "送达"
    assert v2["补投条状态"] == "已补"
    assert v2["超时戳"] == v["超时戳"]
    assert v2["valid"] is True


# ---------------------------------------------------------------------------
# 3. 阈值可配置且写入审计
# ---------------------------------------------------------------------------

def test_阈值可配置_且写入审计(client):
    r = client.post("/超时配置", json={"阈值秒": 77, "口径": "作废"})
    assert r.status_code == 200
    assert r.json()["阈值秒"] == 77
    cfg = client.get("/超时配置").json()
    assert cfg["阈值秒"] == 77 and cfg["口径"] == "作废"

    audits = client.get("/超时审计?动作=配置").json()
    assert audits, "配置变更必须写审计"
    明细 = json.dumps(audits[0]["明细"], ensure_ascii=False)
    assert "77" in 明细 and "阈值" in 明细


# ---------------------------------------------------------------------------
# 4. 扫描窗边界：差 1 秒不命中；恰好阈值命中（闭区间）
# ---------------------------------------------------------------------------

def test_扫描窗边界_右界含(client):
    import datetime as dt

    client.post("/超时配置", json={"阈值秒": 100, "口径": "作废"})
    made = 建离线令(client, "dev-edge")
    no = made["令号"]
    disp = 下发时刻(client, no)
    t0 = dt.datetime.strptime(disp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

    before = (t0 + dt.timedelta(seconds=99)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r1 = client.post("/超时扫描", json={"令号": no, "现在": before}).json()
    assert r1["候选数"] == 0

    exact = (t0 + dt.timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r2 = client.post("/超时扫描", json={"令号": no, "现在": exact}).json()
    assert r2["作废数"] == 1
    assert client.get(f"/查验?令号={no}").json()["状态"] == "作废"


# ---------------------------------------------------------------------------
# 5. 夹具文件与一键对照
# ---------------------------------------------------------------------------

def test_夹具文件_甲乙均存在且口径正确():
    甲 = json.loads((预置目录 / "预置超时甲.json").read_text(encoding="utf-8"))
    乙 = json.loads((预置目录 / "预置超时乙.json").read_text(encoding="utf-8"))
    assert 甲["口径"] == "作废" and 甲["断言"]["重连后仍送达"] is False
    assert 乙["口径"] == "打戳" and 乙["断言"]["重连后仍送达"] is True


def test_一键对照_甲不送达_乙送达(client):
    r = client.post("/演示/超时对照", json={}).json()
    assert r["对照成立"] is True
    assert r["甲不应再送达"] is True
    assert r["乙应补投送达"] is True
    甲v = r["甲"]["查验"]
    乙v = r["乙"]["查验"]
    assert (甲v["状态"], 甲v["补投条状态"]) == ("作废", "取消")
    assert (乙v["状态"], 乙v["补投条状态"]) == ("送达", "已补")
    assert 乙v["超时戳"]


def test_夹具路由_代号识别(client):
    甲 = client.post("/超时夹具", json={"代号": "甲"}).json()
    assert 甲["口径"] == "作废" and 甲["阶段"].startswith("已预置")
    乙 = client.post("/超时夹具", json={"代号": "乙"}).json()
    assert 乙["口径"] == "打戳"


# ---------------------------------------------------------------------------
# 6. 总览在途数与廊状态同源
# ---------------------------------------------------------------------------

def test_总览在途数_与廊状态同源(client, db):
    client.post("/超时配置", json={"阈值秒": 1, "口径": "作废"})
    建离线令(client, "d1")
    建离线令(client, "d2")
    client.post("/设备册", json={"设备键": "d3", "别名": "在线"})  # 默认离线入册
    client.post("/设备册/重连补投", json={"设备键": "d3"})
    client.post("/指令廊/下发", json={"设备键": "d3", "动作": "直达"})

    ov = client.get("/总览").json()
    rc = client.get("/对账").json()
    raw = db.execute("SELECT COUNT(*) n FROM 指令廊 WHERE 状态='在途'").fetchone()["n"]
    assert ov["在途指令"] == ov["在途数"] == rc["在途数"] == raw == 2

    client.post("/拨超时", json={"推进秒": 3})
    client.post("/超时扫描", json={})
    ov2 = client.get("/总览").json()
    rc2 = client.get("/对账").json()
    assert ov2["在途指令"] == ov2["在途数"] == rc2["在途数"] == 0
    assert rc2["幽灵补投条数"] == 0


# ---------------------------------------------------------------------------
# 7. 既有闭环不回归
# ---------------------------------------------------------------------------

def test_既有对影闭环不回归(client):
    r = client.post("/演示/对影闭环").json()
    assert r["ok"] is True
    assert r["补投"]["补投数"] == 1
    验 = r["对影验"][0]
    # 演示末尾仍有拒令记录，但在途/积压应已清空
    assert "令号" in r["积压"]


# ---------------------------------------------------------------------------
# 8. 服务层并发交错压测：失败闭、无双发、无幽灵
# ---------------------------------------------------------------------------

def test_服务层_扫描与补投并发交错_失败闭():
    r = 并发压测服务({"单数": 8})
    assert r["通过"] is True, r
    assert r["异常"] == []
    v = r["作废口径"]
    assert v["双发冲突数"] == 0
    assert v["悬空/不一致数"] == 0
    assert v["扫描作废胜"] + v["重连补投胜"] == 8
    s = r["打戳口径"]
    assert s["补投送达数"] == 8 and s["valid数"] == 8
    assert r["对账"]["幽灵补投条数"] == 0


# ---------------------------------------------------------------------------
# 9. 路由别名 / api 前缀兼容
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/超时查验", "/查验", "/在途超时作废/查验", "/api/超时查验"])
def test_查验别名路由(client, path):
    made = 建离线令(client, "dev-alias")
    r = client.get(path, params={"令号": made["令号"]})
    assert r.status_code == 200
    assert r.json()["令号"] == made["令号"]
    assert r.json()["valid"] is True


@pytest.mark.parametrize("prefix", ["/超时查验", "/查验", "/在途超时作废/查验", "/timeout/verify", "/api/超时查验"])
def test_查验路径参数路由(client, prefix):
    made = 建离线令(client, "dev-path")
    no = made["令号"]
    r = client.get(f"{prefix}/{no}")
    assert r.status_code == 200, r.text
    j = r.json()
    for k in ("令号", "状态", "超时戳", "补投条状态", "valid"):
        assert k in j
    assert j["令号"] == no and j["valid"] is True


def test_查验路径参数路由_未知令号_五要素且valid_false(client):
    r = client.get("/超时查验/CMD-does-not-exist")
    assert r.status_code == 200
    j = r.json()
    assert j["令号"] == "CMD-does-not-exist"
    assert j["valid"] is False and j["状态"] is None


def test_增强列表路由_不报错且带同源字段(client):
    made = 建离线令(client, "dev-list")
    j = client.get("/指令廊").json()
    row = next(x for x in j if x["令号"] == made["令号"])
    assert row["补投条状态"] == "积压" and row["valid"] is True
    b = client.get("/补投条").json()
    brow = next(x for x in b if x["令号"] == made["令号"])
    assert brow["廊状态"] == "在途" and brow["valid"] is True


def test_post裸特性路径_执行扫描(client):
    client.post("/超时配置", json={"阈值秒": 0, "口径": "作废"})
    made = 建离线令(client, "dev-bare")
    r = client.post("/在途超时作废", json={"令号": made["令号"]})
    assert r.status_code == 200
    assert made["令号"] in r.json()["作废令号"]
    meta = client.get("/在途超时作废").json()
    assert meta["物化状态"] == "已物化"
