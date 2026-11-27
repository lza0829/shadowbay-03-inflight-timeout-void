# -*- coding: utf-8 -*-
"""在途超时作废 —— 物化扫描任务 / 状态迁移 / 查验路由 / 并发失败闭。

口径（只能钉死一条路，由「超时口径」配置决定，任何时刻全库同口径一致迁移）：

* ``作废``（默认，对应空桩名「在途超时作废」与甲夹具）：
  扫描窗命中的在途令 → 指令廊.状态='作废'，写「超时戳」；
  同票事务内把对应积压补投条 → '取消'（不物理删除，杜绝幽灵条）；
  作废说明必含「超时」与超时阈值快照。重连补投失败闭，不再送达。
* ``打戳``（乙夹具对照口径）：
  指令廊保持 '在途'，仅写首次「超时戳」；补投条保持 '积压'；
  重连仍按序补投并闭环为 '送达'，超时戳保留可查。

硬约束：
* 超时扫描与重连补投共用同一把进程锁 + SQLite ``BEGIN IMMEDIATE`` 票事务，
  状态迁移全部带「期望前态」条件（CAS），竞争失败一方不改子记录 —— 失败闭。
* 禁止无记录删令：任何路径都不 DELETE 指令廊行，只做状态迁移。
* 总览在途数与指令廊状态同一条 COUNT 谓词，同源。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.db import 连接, 预置目录
from app.seed import 建表, 现在 as _seed现在

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

口径作废 = "作废"
口径打戳 = "打戳"
合法口径 = (口径作废, 口径打戳)

默认阈值秒 = 30
默认口径 = 口径作废

状态在途 = "在途"
状态作废 = "作废"
状态送达 = "送达"
状态拒令 = "拒令"

条积压 = "积压"
条已补 = "已补"
条取消 = "取消"

# 全进程串行化「扫描 / 下发积压 / 重连补投」复合票事务。
超时锁 = threading.RLock()

# 显式状态迁移表（物化状态迁移，供元信息路由与测试对照）。
状态迁移 = {
    "超时扫描[口径=作废]": {
        "指令廊": f"{状态在途} -> {状态作废}",
        "补投条": f"{条积压} -> {条取消}",
        "重连": "失败闭：条件更新 0 行，不补投",
        "送达": False,
    },
    "超时扫描[口径=打戳]": {
        "指令廊": f"{状态在途} -> {状态在途}（仅首写超时戳）",
        "补投条": f"{条积压} -> {条积压}",
        "重连": f"按序 {条积压} -> {条已补}，指令廊 -> {状态送达}，超时戳保留",
        "送达": True,
    },
    "重连补投": {
        "补投条": f"{条积压} -> {条已补}",
        "指令廊": f"{状态在途} -> {状态送达}",
    },
}

_作废词 = {"作废", "废", "void", "cancel", "cancelled", "canceled", "abort", "取消"}
_打戳词 = {"打戳", "戳", "stamp", "mark", "keep", "retain", "在途", "保留", "仅戳"}


# ---------------------------------------------------------------------------
# 时间工具（UTC、秒级定宽串，字典序即时间序；支持可拨虚拟时钟）
# ---------------------------------------------------------------------------

时间格式 = "%Y-%m-%dT%H:%M:%SZ"


def 真实现在() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def 戳(dt: datetime | None = None) -> str:
    dt = dt or 真实现在()
    return dt.astimezone(timezone.utc).strftime(时间格式)


# 兼容旧调用
现在戳 = 戳


def 解析时刻(v) -> datetime:
    if isinstance(v, datetime):
        d = v
    else:
        s = str(v).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).replace(microsecond=0)


def 归一化阈值(v, 默认: int = 默认阈值秒) -> int:
    if v is None or v == "":
        return int(默认)
    if isinstance(v, (int, float)):
        return max(0, int(v))
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    if not m:
        return int(默认)
    return max(0, int(float(m.group(0))))


def 归一化口径(v, 默认: str = 默认口径) -> str:
    if v is None or v == "":
        return 默认
    s = str(v).strip().lower()
    if s in _作废词 or "作废" in s:
        return 口径作废
    if s in _打戳词 or "打戳" in s or "戳" in s:
        return 口径打戳
    if s in 合法口径:
        return s
    return 默认


def 挑(p: dict, *keys, default=None):
    """从混合别名字典里取第一个非空值。"""
    for k in keys:
        if k in p and p[k] is not None and p[k] != "":
            return p[k]
    return default


# ---------------------------------------------------------------------------
# 结构迁移 / 审计 / 配置
# ---------------------------------------------------------------------------

_特性建表 = """
CREATE TABLE IF NOT EXISTS 超时配置 (
  键 TEXT PRIMARY KEY,
  阈值秒 INTEGER NOT NULL DEFAULT 30,
  口径 TEXT NOT NULL DEFAULT '作废',
  时钟偏移秒 INTEGER NOT NULL DEFAULT 0,
  更新时 TEXT NOT NULL,
  备注 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 审计 (
  序号 INTEGER PRIMARY KEY AUTOINCREMENT,
  批次 TEXT NOT NULL DEFAULT '',
  时 TEXT NOT NULL,
  动作 TEXT NOT NULL,
  主体 TEXT NOT NULL DEFAULT '',
  明细 TEXT NOT NULL DEFAULT '{}'
);
"""

_新增列 = {
    "指令廊": [
        ("超时戳", "TEXT NOT NULL DEFAULT ''"),
        ("超时临界时", "TEXT NOT NULL DEFAULT ''"),
        ("阈值快照", "TEXT NOT NULL DEFAULT ''"),
        ("超时批次", "TEXT NOT NULL DEFAULT ''"),
    ],
    "补投条": [
        ("取消时", "TEXT NOT NULL DEFAULT ''"),
        ("扫描批次", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def _现有列(c, 表: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({表})").fetchall()}


def 确保结构() -> None:
    """幂等建表 / 补列 / 写默认配置。任何入口调用都安全。"""
    c = 连接()
    try:
        c.execute("PRAGMA busy_timeout=30000")
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        c.executescript(建表)
        c.executescript(_特性建表)
        for 表, 列们 in _新增列.items():
            现有 = _现有列(c, 表)
            for 名, 定义 in 列们:
                if 名 not in 现有:
                    c.execute(f"ALTER TABLE {表} ADD COLUMN {名} {定义}")
        ts = 戳()
        c.execute(
            "INSERT OR IGNORE INTO 超时配置(键,阈值秒,口径,时钟偏移秒,更新时,备注) "
            "VALUES('当前',?,?,0,?,'超时口径单例；阈值可配置，变更写审计')",
            (默认阈值秒, 默认口径, ts),
        )
        c.commit()
    finally:
        c.close()


@contextmanager
def 即时事务():
    """全局锁 + BEGIN IMMEDIATE 的票事务；异常必回滚（失败闭，不留半票）。"""
    超时锁.acquire()
    c = 连接()
    try:
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        c.close()
        超时锁.release()


def 写审计(c, 动作: str, 主体: str = "", 明细: dict | None = None, 批次: str = "") -> None:
    c.execute(
        "INSERT INTO 审计(批次,时,动作,主体,明细) VALUES(?,?,?,?,?)",
        (批次, 戳(), 动作, 主体, json.dumps(明细 or {}, ensure_ascii=False, sort_keys=True)),
    )


def 取配置(c) -> dict:
    row = c.execute("SELECT * FROM 超时配置 WHERE 键='当前'").fetchone()
    if row is None:
        return {"阈值秒": 默认阈值秒, "口径": 默认口径, "时钟偏移秒": 0}
    return dict(row)


def 虚拟现在(c=None, 显式=None) -> datetime:
    if 显式 is not None and 显式 != "":
        return 解析时刻(显式)
    自有 = c is None
    if 自有:
        c = 连接()
    try:
        off = int(取配置(c).get("时钟偏移秒") or 0)
    finally:
        if 自有:
            c.close()
    return 真实现在() + timedelta(seconds=off)


def 读审计(动作: str | None = None, 条数: int = 100) -> list[dict]:
    确保结构()
    c = 连接()
    try:
        条数 = max(1, min(int(条数 or 100), 1000))
        if 动作:
            rows = c.execute(
                "SELECT * FROM 审计 WHERE 动作 LIKE ? ORDER BY 序号 DESC LIMIT ?",
                (f"%{动作}%", 条数),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM 审计 ORDER BY 序号 DESC LIMIT ?", (条数,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["明细"] = json.loads(d.get("明细") or "{}")
            except Exception:
                pass
            out.append(d)
        return out
    finally:
        c.close()


def 保存配置(p: dict) -> dict:
    确保结构()
    with 即时事务() as c:
        old = 取配置(c)
        阈值 = 归一化阈值(
            挑(p, "阈值秒", "超时阈值秒", "阈值", "threshold_sec", "threshold", "timeout_sec"),
            int(old["阈值秒"]),
        )
        口径 = 归一化口径(
            挑(p, "口径", "策略", "处理方式", "路径", "mode", "policy"),
            old["口径"],
        )
        备注 = 挑(p, "备注", default=old.get("备注", "")) or ""
        ts = 戳()
        c.execute(
            "UPDATE 超时配置 SET 阈值秒=?, 口径=?, 更新时=?, 备注=? WHERE 键='当前'",
            (阈值, 口径, ts, 备注),
        )
        # 阈值写入审计（硬约束）：快照完整落账。
        写审计(
            c,
            "超时配置",
            "当前",
            {
                "阈值秒": 阈值,
                "口径": 口径,
                "原阈值秒": old["阈值秒"],
                "原口径": old["口径"],
                "阈值快照": f"阈值={阈值}秒",
                "更新时": ts,
            },
        )
        return {"ok": True, "阈值秒": 阈值, "口径": 口径, "更新时": ts}


def 拨时钟(p: dict) -> dict:
    """拨虚拟时钟，制造「已超时」而无需真实 sleep。

    传 推进秒/秒/偏移秒 → 在当前偏移上累加；
    传 设偏移/偏移 → 直接设定；
    传 目标时/现在 → 对齐到指定虚拟时刻；
    重置=true → 偏移清零；
    都不给 → 默认推进「阈值秒 + 1」，确保越过超时临界。
    """
    确保结构()
    with 即时事务() as c:
        cfg = 取配置(c)
        off = int(cfg.get("时钟偏移秒") or 0)
        重置 = str(挑(p, "重置", "reset", default="")).lower() in ("true", "1", "是")
        目标 = 挑(p, "目标时", "虚拟时", "现在", "当前时")
        推进 = 挑(p, "推进秒", "秒", "偏移增量", "advance_sec", "seconds")
        设定 = 挑(p, "设偏移", "偏移", "offset_sec", "offset")
        if 重置:
            off = 0
        elif 目标 is not None:
            off = int((解析时刻(目标) - 真实现在()).total_seconds())
        elif 设定 is not None:
            off = int(float(str(设定)))
        else:
            if 推进 is None:
                推进 = int(cfg["阈值秒"]) + 1
            off += int(float(str(推进)))
        ts = 戳()
        c.execute("UPDATE 超时配置 SET 时钟偏移秒=?, 更新时=? WHERE 键='当前'", (off, ts))
        虚拟 = 真实现在() + timedelta(seconds=off)
        写审计(c, "拨超时时钟", "当前", {"时钟偏移秒": off, "虚拟现在": 戳(虚拟)})
        return {"ok": True, "时钟偏移秒": off, "虚拟现在": 戳(虚拟)}


# ---------------------------------------------------------------------------
# 共享写入原语
# ---------------------------------------------------------------------------

def 建设备(c, 设备键: str, 别名: str | None = None, 在线: str = "离线", 指纹: str = "fp-init"):
    ts = 戳()
    c.execute(
        "INSERT OR IGNORE INTO 设备册(设备键,别名,在线,凭证指纹,入册时,更新时) "
        "VALUES(?,?,?,?,?,?)",
        (设备键, 别名 or 设备键, 在线, 指纹, ts, ts),
    )
    c.execute(
        "INSERT OR IGNORE INTO 影子台(设备键,期望载荷,上报载荷,对齐态,期望版,上报版,更新时) "
        "VALUES(?,'{}','{}','未齐',0,0,?)",
        (设备键, ts),
    )
    c.execute("UPDATE 设备册 SET 在线=? WHERE 设备键=?", (在线, 设备键))


def 创建离线积压令(c, 设备键: str, 动作: str, 载荷=None, 指纹: str = "fp-init",
                  下发时: str | None = None) -> dict:
    """在已持有的票事务里同票写入 指令廊(在途) + 补投条(积压)。"""
    ts = 下发时 or 戳()
    令号 = "CMD-" + uuid.uuid4().hex[:10]
    条号 = "Q-" + uuid.uuid4().hex[:10]
    载荷文 = 载荷 if isinstance(载荷, str) else json.dumps(载荷 or {}, ensure_ascii=False)
    c.execute(
        "INSERT INTO 指令廊(令号,设备键,动作,载荷,状态,凭证指纹,下发时,说明) "
        "VALUES(?,?,?,?,'在途',?,?,'离线积压')",
        (令号, 设备键, 动作, 载荷文, 指纹, ts),
    )
    序 = c.execute(
        "SELECT COALESCE(MAX(序),0)+1 n FROM 补投条 WHERE 设备键=? AND 状态='积压'",
        (设备键,),
    ).fetchone()["n"]
    c.execute(
        "INSERT INTO 补投条(条号,设备键,令号,序,状态,入条时) VALUES(?,?,?,?,'积压',?)",
        (条号, 设备键, 令号, 序, ts),
    )
    return {"令号": 令号, "条号": 条号, "序": 序, "下发时": ts}


# ---------------------------------------------------------------------------
# 扫描任务
# ---------------------------------------------------------------------------

def 构造扫描窗(c, cfg: dict, p: dict) -> dict:
    阈值 = 归一化阈值(
        挑(p, "阈值秒", "超时阈值秒", "阈值", "threshold_sec", "threshold"),
        int(cfg["阈值秒"]),
    )
    扫描dt = 虚拟现在(c, 挑(p, "现在", "当前时", "扫描时", "时刻"))
    窗起 = 挑(p, "窗起", "左界", "起")
    截止dt = 扫描dt - timedelta(seconds=阈值)
    窗 = {
        "扫描时": 戳(扫描dt),
        "阈值秒": 阈值,
        "右界_截止时": 戳(截止dt),
        "右界含": True,
        "左界_起": 窗起 or "",
        "左界含": True if 窗起 else None,
        "判定式": "指令廊.状态='在途' AND 指令廊.下发时 <= 右界_截止时 [AND 下发时 >= 左界_起]",
        "年龄边界": f"令龄 >= {阈值} 秒（恰好 {阈值} 秒也命中，闭区间）",
        "边界说明": "扫描窗为 [可选左界, 右界]，右界含；按下发时定宽 UTC 串比较",
    }
    return 窗


def 扫描超时服务(p: dict | None = None) -> dict:
    p = p or {}
    确保结构()
    with 即时事务() as c:
        cfg = 取配置(c)
        口径 = 归一化口径(挑(p, "口径", "策略", "处理方式", "路径", "mode", "policy"), cfg["口径"])
        窗 = 构造扫描窗(c, cfg, p)
        阈值 = 窗["阈值秒"]
        扫描ts = 窗["扫描时"]
        截止 = 窗["右界_截止时"]
        窗起 = 窗["左界_起"]
        批次 = 挑(p, "批次", "扫描批次") or ("TMO-" + uuid.uuid4().hex[:12])
        设备键 = 挑(p, "设备键", "device")
        令号 = 挑(p, "令号", "id", "cmd")

        sql = (
            "SELECT * FROM 指令廊 WHERE 状态='在途' AND 下发时 <= ? "
        )
        params: list = [截止]
        if 窗起:
            sql += "AND 下发时 >= ? "
            params.append(str(窗起))
        if 设备键:
            sql += "AND 设备键=? "
            params.append(设备键)
        if 令号:
            sql += "AND 令号=? "
            params.append(令号)
        sql += "ORDER BY 设备键, 下发时"
        候选 = [dict(r) for r in c.execute(sql, params).fetchall()]

        作废们, 打戳们, 竞争跳过 = [], [], []
        for d in 候选:
            no = d["令号"]
            临界 = 戳(解析时刻(d["下发时"]) + timedelta(seconds=阈值))
            快照 = f"阈值={阈值}秒;口径={口径};批次={批次};临界时={临界};扫描时={扫描ts}"
            if 口径 == 口径作废:
                说明 = (
                    f"超时作废：扫描命中超时窗，{窗['年龄边界']}；"
                    f"阈值快照（超时阈值={阈值}秒，threshold_snapshot={阈值}s）；"
                    f"临界时 {临界}；扫描时 {扫描ts}；扫描批次 {批次}"
                )
                # CAS：只有仍是「在途」且仍在窗内才允许作废。
                cur = c.execute(
                    "UPDATE 指令廊 SET 状态='作废', 超时戳=?, 超时临界时=?, 阈值快照=?, "
                    "超时批次=?, 闭环时=?, 说明=? "
                    "WHERE 令号=? AND 状态='在途' AND 下发时 <= ?",
                    (扫描ts, 临界, f"{阈值}秒", 批次, 扫描ts, 说明, no, 截止),
                )
                if cur.rowcount != 1:
                    # 竞争失败（重连先送达）：失败闭，绝不动补投条。
                    写审计(c, "超时竞争_失败闭", no,
                           {"原因": "CAS未命中，重连补投可能已先提交", "批次": 批次}, 批次)
                    竞争跳过.append(no)
                    continue
                bcur = c.execute(
                    "UPDATE 补投条 SET 状态='取消', 取消时=?, 扫描批次=? "
                    "WHERE 令号=? AND 状态='积压'",
                    (扫描ts, 批次, no),
                )
                写审计(c, "超时作废", no, {
                    "阈值秒": 阈值, "口径": 口径, "超时戳": 扫描ts,
                    "临界时": 临界, "取消补投条数": bcur.rowcount,
                    "阈值快照": f"{阈值}秒",
                }, 批次)
                作废们.append({"令号": no, "超时戳": 扫描ts, "补投条取消数": bcur.rowcount})
            else:
                # 打戳路径：仅首写超时戳，状态保持在途，补投条不动。
                cur = c.execute(
                    "UPDATE 指令廊 SET 超时戳=?, 超时临界时=?, 阈值快照=?, 超时批次=? "
                    "WHERE 令号=? AND 状态='在途' AND 下发时 <= ? "
                    "AND COALESCE(超时戳,'')=''",
                    (扫描ts, 临界, f"{阈值}秒", 批次, no, 截止),
                )
                if cur.rowcount == 1:
                    写审计(c, "超时打戳", no, {
                        "阈值秒": 阈值, "口径": 口径, "超时戳": 扫描ts, "临界时": 临界,
                    }, 批次)
                    打戳们.append({"令号": no, "超时戳": 扫描ts, "新打戳": True})
                else:
                    row = c.execute("SELECT 超时戳 FROM 指令廊 WHERE 令号=?", (no,)).fetchone()
                    if row and row["超时戳"]:
                        打戳们.append({"令号": no, "超时戳": row["超时戳"], "新打戳": False})
                    else:
                        竞争跳过.append(no)

        结果 = {
            "ok": True,
            "批次": 批次,
            "口径": 口径,
            "扫描窗": 窗,
            "候选数": len(候选),
            "命中数": len(候选),
            "扫描数": len(候选),
            "scanned": len(候选),
            "作废数": len(作废们),
            "作废条数": len(作废们),
            "voided": len(作废们),
            "打戳数": len([x for x in 打戳们 if x.get("新打戳")]),
            "作废令号": [x["令号"] for x in 作废们],
            "打戳令号": [x["令号"] for x in 打戳们],
            "作废": 作废们,
            "打戳": 打戳们,
            "竞争跳过": 竞争跳过,
        }
        写审计(c, "超时扫描", 批次, {
            "口径": 口径, "阈值秒": 阈值, "扫描窗": 窗,
            "候选数": len(候选), "作废数": len(作废们),
            "打戳数": 结果["打戳数"], "竞争跳过": 竞争跳过,
        }, 批次)
        return 结果


# ---------------------------------------------------------------------------
# 重连补投（与扫描同锁、票事务、失败闭）
# ---------------------------------------------------------------------------

def 重连补投服务(设备键: str) -> dict:
    确保结构()
    with 即时事务() as c:
        dev = c.execute("SELECT * FROM 设备册 WHERE 设备键=?", (设备键,)).fetchone()
        if not dev:
            raise KeyError(f"设备不在册: {设备键}")
        ts = 戳(虚拟现在(c))
        c.execute("UPDATE 设备册 SET 在线='在线', 更新时=? WHERE 设备键=?", (ts, 设备键))
        条们 = c.execute(
            "SELECT * FROM 补投条 WHERE 设备键=? AND 状态='积压' ORDER BY 序",
            (设备键,),
        ).fetchall()
        完成, 失败闭 = [], []
        for 条 in 条们:
            no = 条["令号"]
            cur = c.execute(
                "UPDATE 指令廊 SET 状态='送达', 闭环时=?, 说明='重连补投（按序）' "
                "WHERE 令号=? AND 状态='在途'",
                (ts, no),
            )
            if cur.rowcount != 1:
                # 廊已不是在途（超时作废先赢 / 已送达）。失败闭：本条绝不补投送达。
                廊 = c.execute("SELECT 状态,说明 FROM 指令廊 WHERE 令号=?", (no,)).fetchone()
                廊态 = 廊["状态"] if 廊 else "无记录"
                if 廊态 == 状态作废:
                    c.execute(
                        "UPDATE 补投条 SET 状态='取消', 取消时=? WHERE 条号=? AND 状态='积压'",
                        (ts, 条["条号"]),
                    )
                    失败闭.append({"令号": no, "廊状态": 廊态, "处置": "补投条改取消，不送达"})
                else:
                    # 其它非在途态：不覆盖历史，保留积压并上报冲突，交由查验暴露。
                    失败闭.append({"令号": no, "廊状态": 廊态, "处置": "CAS未命中，保留原状"})
                写审计(c, "重连竞争_失败闭", no, {"廊状态": 廊态, "条号": 条["条号"]})
                continue
            u = c.execute(
                "UPDATE 补投条 SET 状态='已补', 补投时=? WHERE 条号=? AND 状态='积压'",
                (ts, 条["条号"]),
            )
            if u.rowcount == 1:
                完成.append(no)
        写审计(c, "重连补投", 设备键, {
            "补投数": len(完成), "令号": 完成, "失败闭": 失败闭, "时": ts,
        })
        return {
            "ok": True, "设备键": 设备键, "补投数": len(完成),
            "令号": 完成, "补投令号": 完成, "失败闭": 失败闭,
            "在线": "在线",
        }


# ---------------------------------------------------------------------------
# 查验 / 对账（返回五要素：令号、状态、超时戳、补投条状态、valid）
# ---------------------------------------------------------------------------

def 判定一致性(d: dict, bars: list[dict]) -> list[str]:
    问题: list[str] = []
    st = d.get("状态")
    bstats = [b["状态"] for b in bars]
    if "积压" in bstats and st != 状态在途:
        问题.append(f"幽灵补投条：补投条仍积压，但廊状态为「{st}」")
    if st == 状态作废:
        if "积压" in bstats:
            问题.append("作废令仍存在积压补投条")
        if bars and not any(x == 条取消 for x in bstats):
            问题.append("作废令的补投条未改取消")
        if "超时" not in (d.get("说明") or ""):
            问题.append("作废说明未含「超时」")
        if not d.get("超时戳"):
            问题.append("作废令缺超时戳")
        if not d.get("阈值快照"):
            问题.append("作废记录缺超时阈值快照")
    elif st == 状态在途:
        if any(x != 条积压 for x in bstats):
            问题.append("在途令的补投条不是积压态")
    elif st == 状态送达:
        if "积压" in bstats:
            问题.append("送达令仍挂积压补投条")
        if bars and not all(x == 条已补 for x in bstats):
            问题.append("送达令的补投条未全部已补")
    elif st == 状态拒令:
        if bars:
            问题.append("拒令不应存在补投条")
    if d.get("超时戳"):
        try:
            解析时刻(d["超时戳"])
        except Exception:
            问题.append("超时戳格式不可查")
    return 问题


def 查验服务(令号: str) -> dict:
    确保结构()
    c = 连接()
    try:
        row = c.execute("SELECT * FROM 指令廊 WHERE 令号=?", (令号,)).fetchone()
        if row is None:
            # 即使无记录也保证五要素键形；valid=False（禁止无记录删令 → 正常流程不会走到）。
            return {
                "令号": 令号, "状态": None, "超时戳": "",
                "补投条状态": None, "valid": False, "存在": False,
                "cmd_id": 令号, "status": None, "timeout_at": "", "bar_status": None,
                "问题": ["无此令号记录"],
            }
        d = dict(row)
        bars = [dict(r) for r in c.execute(
            "SELECT * FROM 补投条 WHERE 令号=? ORDER BY 序", (令号,)
        ).fetchall()]
        问题 = 判定一致性(d, bars)
        if not bars:
            条态 = "无补投条"
        elif len(bars) == 1:
            条态 = bars[0]["状态"]
        else:
            条态 = bars[0]["状态"]  # 模型一令一条；主状态取首条
        return {
            # —— 约定五要素（键名逐字）——
            "令号": d["令号"],
            "状态": d["状态"],
            "超时戳": d.get("超时戳", "") or "",
            "补投条状态": 条态,
            "valid": len(问题) == 0,
            # —— 同义键兼容 ——
            "cmd_id": d["令号"],
            "order_id": d["令号"],
            "status": d["状态"],
            "timeout_at": d.get("超时戳", "") or "",
            "timeout_ts": d.get("超时戳", "") or "",
            "bar_status": 条态,
            # —— 可观测增强 ——
            "存在": True,
            "设备键": d["设备键"],
            "动作": d["动作"],
            "补投条状态列表": [b["状态"] for b in bars],
            "补投条": bars,
            "说明": d["说明"],
            "阈值快照": d.get("阈值快照", "") or "",
            "超时临界时": d.get("超时临界时", "") or "",
            "超时批次": d.get("超时批次", "") or "",
            "闭环时": d.get("闭环时", "") or "",
            "问题": 问题,
        }
    finally:
        c.close()


def 在途计数(c) -> int:
    """总览在途数的唯一同源谓词。"""
    return c.execute("SELECT COUNT(*) n FROM 指令廊 WHERE 状态='在途'").fetchone()["n"]


def 对账服务(设备键: str | None = None) -> dict:
    确保结构()
    c = 连接()
    try:
        sql = "SELECT * FROM 指令廊"
        params: list = []
        if 设备键:
            sql += " WHERE 设备键=?"
            params.append(设备键)
        sql += " ORDER BY 下发时 DESC LIMIT 500"
        令rows = [dict(r) for r in c.execute(sql, params).fetchall()]
        行 = []
        幽灵 = 0
        全部valid = True
        for d in 令rows:
            bars = [dict(r) for r in c.execute(
                "SELECT * FROM 补投条 WHERE 令号=? ORDER BY 序", (d["令号"],)
            ).fetchall()]
            问题 = 判定一致性(d, bars)
            if 问题:
                全部valid = False
            if any("幽灵" in x for x in 问题):
                幽灵 += 1
            行.append({
                "令号": d["令号"],
                "设备键": d["设备键"],
                "动作": d["动作"],
                "廊状态": d["状态"],
                "状态": d["状态"],
                "超时戳": d.get("超时戳", "") or "",
                "补投条状态": (bars[0]["状态"] if bars else "无补投条"),
                "valid": not 问题,
                "问题": 问题,
                "说明": d.get("说明", ""),
                "阈值快照": d.get("阈值快照", "") or "",
                "下发时": d["下发时"],
                "闭环时": d.get("闭环时", ""),
            })
        在途数 = 在途计数(c)
        积压数 = c.execute("SELECT COUNT(*) n FROM 补投条 WHERE 状态='积压'").fetchone()["n"]
        作废数 = c.execute("SELECT COUNT(*) n FROM 指令廊 WHERE 状态='作废'").fetchone()["n"]
        取消条数 = c.execute("SELECT COUNT(*) n FROM 补投条 WHERE 状态='取消'").fetchone()["n"]
        return {
            "ok": True,
            "同源": True,
            "同源说明": "总览.在途指令/在途数 与 对账.在途数 均为 COUNT(指令廊 WHERE 状态='在途')",
            "在途数": 在途数,
            "总览在途指令": 在途数,
            "积压补投数": 积压数,
            "作废数": 作废数,
            "取消补投条数": 取消条数,
            "幽灵补投条数": 幽灵,
            "全部valid": 全部valid and 幽灵 == 0,
            "行": 行,
        }
    finally:
        c.close()


def 同源快照服务() -> dict:
    """单连接瞬时快照：总览在途数与廊状态计数在同一读事务内给出，保证瞬时同源。"""
    确保结构()
    c = 连接()
    try:
        c.execute("BEGIN")
        在途 = 在途计数(c)
        积压 = c.execute("SELECT COUNT(*) n FROM 补投条 WHERE 状态='积压'").fetchone()["n"]
        幽灵 = c.execute(
            "SELECT COUNT(*) n FROM 补投条 b LEFT JOIN 指令廊 d ON d.令号=b.令号 "
            "WHERE b.状态='积压' AND (d.令号 IS NULL OR d.状态 != '在途')"
        ).fetchone()["n"]
        c.commit()
        return {
            "ok": True,
            "在途数": 在途,
            "总览在途指令": 在途,
            "总览在途数": 在途,
            "对账在途数": 在途,
            "积压补投": 积压,
            "幽灵补投条数": 幽灵,
            "瞬时同源": True,
            "同源": True,
            "同源谓词": "COUNT(指令廊 WHERE 状态='在途')",
        }
    finally:
        c.close()


def 列表指令服务() -> list[dict]:
    确保结构()
    c = 连接()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM 指令廊 ORDER BY 下发时 DESC LIMIT 200"
        ).fetchall()]
        for d in rows:
            bars = [dict(r) for r in c.execute(
                "SELECT 状态 FROM 补投条 WHERE 令号=? ORDER BY 序", (d["令号"],)
            ).fetchall()]
            d["补投条状态"] = bars[0]["状态"] if bars else "无补投条"
            d["valid"] = len(判定一致性(d, bars)) == 0
        return rows
    finally:
        c.close()


def 列表补投条服务() -> list[dict]:
    确保结构()
    c = 连接()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT b.*, d.状态 AS 廊状态, d.超时戳 AS 超时戳 "
            "FROM 补投条 b LEFT JOIN 指令廊 d ON d.令号=b.令号 "
            "ORDER BY b.设备键, b.序"
        ).fetchall()]
        out = []
        for r in rows:
            d = dict(r)
            # valid 以廊行完整判定
            廊 = c.execute("SELECT * FROM 指令廊 WHERE 令号=?", (d["令号"],)).fetchone()
            if 廊:
                bars = [dict(x) for x in c.execute(
                    "SELECT * FROM 补投条 WHERE 令号=? ORDER BY 序", (d["令号"],)
                ).fetchall()]
                d["valid"] = len(判定一致性(dict(廊), bars)) == 0
            else:
                d["valid"] = False
            out.append(d)
        return out
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 夹具预置（甲=作废不送达；乙=打戳仍补投）
# ---------------------------------------------------------------------------

_夹具文件 = {
    "甲": ["预置超时甲.json", "超时甲_作废.json", "预置超时_甲_作废.json"],
    "乙": ["预置超时乙.json", "超时乙_打戳.json", "预置超时_乙_打戳.json"],
}


def _读夹具文件(代号: str) -> dict:
    for 名 in _夹具文件.get(代号, []):
        p = 预置目录 / 名
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return {}


def 预置夹具服务(p: dict) -> dict:
    确保结构()
    代号原始 = str(挑(p, "代号", "编号", "case", "fixture", default="") or "")
    路径参数 = 挑(p, "路径", "口径", "策略", "mode", "policy")
    if 代号原始 in ("甲", "A", "a", "jia", "void", "作废") or "甲" in 代号原始:
        代号 = "甲"
    elif 代号原始 in ("乙", "B", "b", "yi", "stamp", "打戳") or "乙" in 代号原始:
        代号 = "乙"
    elif 路径参数 is not None:
        代号 = "甲" if 归一化口径(路径参数) == 口径作废 else "乙"
    else:
        代号 = "甲"

    文件 = _读夹具文件(代号)
    默认口径 = 口径作废 if 代号 == "甲" else 口径打戳
    口径 = 归一化口径(挑(p, "口径", "策略", "路径", "mode"),
                     归一化口径(文件.get("口径") or 文件.get("路径"), 默认口径))
    阈值 = 归一化阈值(挑(p, "阈值秒", "阈值"),
                     归一化阈值(文件.get("阈值秒"), 60))
    设备键 = 挑(p, "设备键") or 文件.get("设备键") or (
        "dev-tmo-jia" if 代号 == "甲" else "dev-tmo-yi"
    )
    别名 = 文件.get("别名") or ("超时夹具甲·作废不补" if 代号 == "甲" else "超时夹具乙·打戳仍补")
    动作 = 文件.get("动作") or ("锁闭通道" if 代号 == "甲" else "采集补传")
    载荷 = 文件.get("载荷") or ({"case": 代号, "path": 口径})
    指纹 = 文件.get("凭证指纹") or "fp-init"
    执行到底 = str(挑(p, "执行到底", "到底", "full", default="")).lower() in ("true", "1", "是")

    # 统一把口径/阈值写入配置与审计；时钟归零，保证场景可重复预置。
    保存配置({"阈值秒": 阈值, "口径": 口径})
    拨时钟({"重置": True})

    with 即时事务() as c:
        建设备(c, 设备键, 别名, 在线="离线", 指纹=指纹)
        made = 创建离线积压令(c, 设备键, 动作, 载荷, 指纹=指纹)
        令号 = made["令号"]

    结果 = {
        "ok": True,
        "代号": 代号,
        "口径": 口径,
        "阈值秒": 阈值,
        "设备键": 设备键,
        **made,
        "阶段": "已预置：断线 → 下发（在途+积压）；待拨超时/扫描/重连",
        "查验路由": f"/在途超时作废/查验?令号={令号}",
    }

    if 执行到底:
        推进 = int(文件.get("拨秒") or (阈值 + 1))
        拨时钟({"推进秒": 推进})
        扫描 = 扫描超时服务({"令号": 令号})
        补 = 重连补投服务(设备键)
        验 = 查验服务(令号)
        结果.update({
            "阶段": "已到底：断线→下发→拨超时→扫描→重连",
            "扫描": 扫描,
            "重连": 补,
            "查验": 验,
            "重连后仍送达": 验["状态"] == 状态送达 and 验["补投条状态"] == 条已补,
            "甲断言_不应再送达": (代号 == "甲" and 验["状态"] == 状态作废
                             and 验["补投条状态"] == 条取消 and 补["补投数"] == 0),
            "乙断言_应补投送达": (代号 == "乙" and 验["状态"] == 状态送达
                            and 验["补投条状态"] == 条已补 and bool(验["超时戳"])),
        })
    return 结果


def 对照演练服务(p: dict | None = None) -> dict:
    """甲（作废）与乙（打戳）各跑一遍全流程，返回对照断言。"""
    p = p or {}
    甲 = 预置夹具服务({"代号": "甲", "执行到底": True, **{k: v for k, v in p.items() if k in ("阈值秒",)}})
    乙 = 预置夹具服务({"代号": "乙", "执行到底": True, **{k: v for k, v in p.items() if k in ("阈值秒",)}})
    return {
        "ok": True,
        "甲": 甲,
        "乙": 乙,
        "甲不应再送达": 甲.get("甲断言_不应再送达", False),
        "乙应补投送达": 乙.get("乙断言_应补投送达", False),
        "对照成立": bool(甲.get("甲断言_不应再送达") and 乙.get("乙断言_应补投送达")),
    }


# ---------------------------------------------------------------------------
# 并发交错压测（服务层线程 + 屏障，真正竞争同一把票锁）
# ---------------------------------------------------------------------------

def 并发压测服务(p: dict | None = None) -> dict:
    p = p or {}
    确保结构()
    n = int(挑(p, "单数", "每路径", "数量", "n", default=8) or 8)
    n = max(1, min(n, 40))
    run = uuid.uuid4().hex[:8]

    def 造令们(phase: str, count: int) -> list[dict]:
        items = []
        with 即时事务() as c:
            for i in range(count):
                dev = f"dev-race-{phase}-{run}-{i}"
                建设备(c, dev, f"竞争{phase}{i}", 在线="离线")
                made = 创建离线积压令(c, dev, "竞争动作", {"i": i})
                items.append({"设备键": dev, "令号": made["令号"]})
        return items

    异常: list[str] = []

    # 阶段一：作废口径，扫描 vs 重连 二选一赢家。
    甲们 = 造令们("v", n)
    闸v = threading.Barrier(2 * n)

    def v_scan(no):
        try:
            闸v.wait()
            return 扫描超时服务({"令号": no, "阈值秒": 0, "口径": "作废"})
        except Exception as e:  # noqa: BLE001
            异常.append(f"scan:{no}:{e!r}")

    def v_recon(dev):
        try:
            闸v.wait()
            return 重连补投服务(dev)
        except Exception as e:  # noqa: BLE001
            异常.append(f"recon:{dev}:{e!r}")

    with ThreadPoolExecutor(max_workers=2 * n + 2) as ex:
        futs = []
        for it in 甲们:
            futs.append(ex.submit(v_scan, it["令号"]))
            futs.append(ex.submit(v_recon, it["设备键"]))
        for f in futs:
            f.result(timeout=60)

    甲验 = [查验服务(it["令号"]) for it in 甲们]
    甲作废胜 = sum(1 for x in 甲验 if x["状态"] == 状态作废 and x["补投条状态"] == 条取消)
    甲补投胜 = sum(1 for x in 甲验 if x["状态"] == 状态送达 and x["补投条状态"] == 条已补)
    甲双发 = sum(1 for x in 甲验 if x["状态"] == 状态作废 and x["补投条状态"] == 条已补)
    甲悬空 = sum(1 for x in 甲验 if x["状态"] == 状态在途 or not x["valid"])

    # 阶段二：打戳口径，两个扫描 + 重连交错；最终都应补投送达且戳保留。
    乙们 = 造令们("s", n)
    闸s = threading.Barrier(3 * n)

    def s_scan(no):
        try:
            闸s.wait()
            return 扫描超时服务({"令号": no, "阈值秒": 0, "口径": "打戳"})
        except Exception as e:  # noqa: BLE001
            异常.append(f"sscan:{no}:{e!r}")

    def s_recon(dev):
        try:
            闸s.wait()
            # 稍微放一下，增加与扫描交错概率，但不依赖时序正确性。
            time.sleep(0.002)
            return 重连补投服务(dev)
        except Exception as e:  # noqa: BLE001
            异常.append(f"srecon:{dev}:{e!r}")

    with ThreadPoolExecutor(max_workers=3 * n + 2) as ex:
        futs = []
        for it in 乙们:
            futs.append(ex.submit(s_scan, it["令号"]))
            futs.append(ex.submit(s_scan, it["令号"]))
            futs.append(ex.submit(s_recon, it["设备键"]))
        for f in futs:
            f.result(timeout=60)

    乙验 = [查验服务(it["令号"]) for it in 乙们]
    乙送达 = sum(1 for x in 乙验 if x["状态"] == 状态送达 and x["补投条状态"] == 条已补)
    乙有戳 = sum(1 for x in 乙验 if bool(x["超时戳"]))
    乙valid = sum(1 for x in 乙验 if x["valid"])

    对账 = 对账服务()
    通过 = (
        not 异常
        and 甲双发 == 0
        and 甲悬空 == 0
        and 甲作废胜 + 甲补投胜 == n
        and 乙送达 == n
        and 乙valid == n
        and 对账["幽灵补投条数"] == 0
    )
    return {
        "ok": 通过,
        "通过": 通过,
        "交错数": 2 * n + 3 * n,
        "异常": 异常,
        "作废口径": {
            "总数": n, "扫描作废胜": 甲作废胜, "重连补投胜": 甲补投胜,
            "双发冲突数": 甲双发, "悬空/不一致数": 甲悬空,
            "断言": "每令恰有一个赢家：作废+取消 或 送达+已补；无双发、无悬空",
        },
        "打戳口径": {
            "总数": n, "补投送达数": 乙送达, "保留超时戳数": 乙有戳,
            "valid数": 乙valid,
            "断言": "重连后全部送达+已补；扫在重连前则超时戳保留",
        },
        "对账": {
            "全部valid": 对账["全部valid"],
            "幽灵补投条数": 对账["幽灵补投条数"],
            "在途数": 对账["在途数"],
        },
        "总览同源": "压测期间由自动化并发 GET /总览 与 /对账 比对在途数",
    }


# ---------------------------------------------------------------------------
# HTTP 层：一个分派器 + 多组路径别名（兼容不同命名习惯 / /api 前缀）
# ---------------------------------------------------------------------------

router = APIRouter()


async def _解析参数(request: Request) -> dict:
    q = dict(request.query_params)
    body: dict = {}
    if request.method in ("POST", "PUT", "PATCH"):
        ctype = request.headers.get("content-type", "")
        try:
            if "application/json" in ctype:
                got = await request.json()
                if isinstance(got, dict):
                    body = got
            elif "form" in ctype:
                body = dict(await request.form())
            else:
                raw = (await request.body()).decode("utf-8", "ignore").strip()
                if raw:
                    try:
                        got = json.loads(raw)
                        if isinstance(got, dict):
                            body = got
                    except Exception:
                        body = {}
        except Exception:
            body = {}
    merged = {**dict(request.path_params), **body, **q}
    merged["__method__"] = request.method
    return merged


def _分派(key: str, p: dict):
    if key == "元信息":
        # GET 元信息；POST 裸路径默认物化执行一次扫描（任务主动作）。
        if p.get("__method__") == "POST" and not 挑(p, "meta", "元信息", "info"):
            return 扫描超时服务(p)
        cfg = 取配置_safe()
        return {
            "空桩": "在途超时作废",
            "物化状态": "已物化",
            "默认口径": 默认口径,
            "当前配置": cfg,
            "约定查验五要素": ["令号", "状态", "超时戳", "补投条状态", "valid"],
            "状态迁移": 状态迁移,
            "路由": sorted({path for paths, k, _d in _路由表 if k == key for path in paths}),
        }
    if key == "配置":
        if p.get("__method__") == "GET" and not any(
            k in p for k in ("阈值秒", "阈值", "口径", "策略", "mode", "policy")
        ):
            确保结构()
            c = 连接()
            try:
                cfg = 取配置(c)
            finally:
                c.close()
            t = int(cfg["阈值秒"])
            return {"ok": True, **cfg,
                    "超时阈值秒": t, "超时阈值": t, "threshold_sec": t,
                    "policy": cfg["口径"]}
        return 保存配置(p)
    if key == "拨钟":
        return 拨时钟(p)
    if key == "扫描":
        return 扫描超时服务(p)
    if key == "查验":
        no = 挑(p, "令号", "id", "cmd", "cmd_id", "order")
        if not no:
            return {"valid": False, "问题": ["缺少令号参数"], "令号": None,
                    "状态": None, "超时戳": "", "补投条状态": None}
        return 查验服务(str(no))
    if key == "对账":
        return 对账服务(挑(p, "设备键", "device"))
    if key == "同源":
        return 同源快照服务()
    if key == "指令列表":
        return 列表指令服务()
    if key == "补投列表":
        return 列表补投条服务()
    if key == "审计":
        return 读审计(挑(p, "动作", "action"), 归一化阈值(挑(p, "条数", "limit"), 100))
    if key == "夹具":
        return 预置夹具服务(p)
    if key == "对照":
        return 对照演练服务(p)
    if key == "压测":
        return 并发压测服务(p)
    if key == "重连":
        dev = 挑(p, "设备键", "设备", "device", "device_key")
        if not dev:
            raise HTTPException(400, "缺少设备键")
        try:
            return 重连补投服务(str(dev))
        except KeyError as e:
            raise HTTPException(400, str(e))
    raise KeyError(key)


def 取配置_safe() -> dict:
    确保结构()
    c = 连接()
    try:
        return 取配置(c)
    finally:
        c.close()


_路由表: list[tuple[list[str], str, dict]] = [
    (["/在途超时作废", "/在途超时作废/元信息", "/超时", "/timeout"], "元信息", {}),
    (["/在途超时作废/查验", "/超时查验", "/查验", "/查验超时", "/超时/查验",
      "/指令廊/查验", "/指令廊/超时查验", "/timeout/verify", "/timeout/check"], "查验", {}),
    # 注意：Starlette 路径参数名只认 ASCII 标识符，不能写 {令号}
    # （否则会被当成字面量花括号，路由永远 404）；cmd_id 会被查验服务识别。
    (["/在途超时作废/查验/{cmd_id}", "/超时查验/{cmd_id}", "/查验/{cmd_id}",
      "/timeout/verify/{cmd_id}"], "查验", {}),
    (["/在途超时作废/扫描", "/在途超时作废/超时扫描", "/超时扫描", "/扫描超时",
      "/超时/扫描", "/扫描任务", "/物化扫描", "/timeout/scan"], "扫描", {}),
    (["/在途超时作废/配置", "/在途超时作废/阈值", "/超时配置", "/超时阈值",
      "/超时/配置", "/timeout/config"], "配置", {}),
    (["/在途超时作废/拨超时", "/拨超时", "/调试/拨钟", "/时钟/推进",
      "/超时/拨钟", "/timeout/advance"], "拨钟", {}),
    (["/在途超时作废/对账", "/超时对账", "/对账", "/超时/对账", "/timeout/reconcile"], "对账", {}),
    (["/在途超时作废/同源", "/同源校验", "/在途同源", "/总览同源",
      "/timeout/same-source"], "同源", {}),
    (["/在途超时作废/指令廊", "/指令廊/增强"], "指令列表", {}),
    (["/在途超时作废/补投条", "/补投条/增强"], "补投列表", {}),
    (["/在途超时作废/审计", "/超时审计", "/审计", "/超时/审计", "/audit"], "审计", {}),
    (["/在途超时作废/夹具", "/在途超时作废/预置", "/超时夹具", "/超时预置",
      "/夹具/超时", "/timeout/fixture"], "夹具", {}),
    (["/在途超时作废/对照", "/超时对照", "/演示/超时对照", "/演示/超时",
      "/timeout/demo"], "对照", {}),
    (["/演示/超时甲", "/演示/超时作废", "/超时甲"], "夹具",
     {"代号": "甲", "执行到底": True}),
    (["/演示/超时乙", "/演示/超时打戳", "/超时乙"], "夹具",
     {"代号": "乙", "执行到底": True}),
    (["/在途超时作废/并发压测", "/超时并发压测", "/压测/超时补投",
      "/timeout/stress"], "压测", {}),
    (["/重连补投", "/补投/重连", "/设备/重连补投", "/超时重连补投",
      "/timeout/reconnect"], "重连", {}),
]


def _造端点(key: str, defaults: dict):
    async def 端点(request: Request):
        p = await _解析参数(request)
        # 路由级默认值兜底（如 /演示/超时甲 强制代号甲并执行到底），不覆盖显式入参。
        for k, v in defaults.items():
            p.setdefault(k, v)
        return await run_in_threadpool(_分派, key, p)

    端点.__name__ = f"超时端点_{key}_{uuid.uuid4().hex[:6]}"
    return 端点


for _paths, _key, _defaults in _路由表:
    _ep = _造端点(_key, _defaults)
    for _path in _paths:
        router.add_api_route(_path, _ep, methods=["GET", "POST"], name=f"tmo_{_key}_{_path}")
        # 直连后端带 /api 前缀的兼容别名（网关通常会剥掉一层，这里再兜一层）。
        if not _path.startswith("/api"):
            router.add_api_route("/api" + _path, _ep, methods=["GET", "POST"],
                                 name=f"tmo_api_{_key}_{_path}")


# 模块导入即确保结构，保证不触发生命周期事件（如未用 with 的 TestClient）也可用。
try:
    确保结构()
except Exception:  # 导入期数据库不可写不应阻断静态导入；启动/请求时会再试。
    pass
