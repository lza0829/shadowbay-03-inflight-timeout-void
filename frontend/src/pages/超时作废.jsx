import React, { useCallback, useEffect, useRef, useState } from "react";
import { get, post } from "../api.js";

function 状态色(s) {
  if (s === "送达" || s === "已补") return "好";
  if (s === "作废" || s === "取消" || s === "拒令") return "坏";
  if (s === "在途" || s === "积压") return "警";
  return "淡";
}

function 徽章({ ok, yes = "valid", no = "invalid" }) {
  return <span className={"徽章 " + (ok ? "徽好" : "徽坏")}>{ok ? yes : no}</span>;
}

const 空查验 = { 令号: "", 状态: null, 超时戳: "", 补投条状态: null, valid: false };

export default function 超时作废() {
  const [cfg, setCfg] = useState({ 阈值秒: 60, 口径: "作废" });
  const [阈值输入, set阈值输入] = useState(60);
  const [口径输入, set口径输入] = useState("作废");
  const [设备键, set设备键] = useState("dev-tmo-jia");
  const [令号输入, set令号输入] = useState("");
  const [查验, set查验] = useState(空查验);
  const [账, set账] = useState({ 行: [], 全部valid: true });
  const [源, set源] = useState(null);
  const [讯, set讯] = useState("");
  const [忙, set忙] = useState(false);
  const [压测, set压测] = useState(null);
  const [对照, set对照] = useState(null);
  const 计时器 = useRef(null);

  const 刷账 = useCallback(async () => {
    const [d, s] = await Promise.all([get("/对账"), get("/同源校验")]);
    set账(d);
    set源(s);
  }, []);

  const 刷配置 = useCallback(() => get("/超时配置").then((c) => {
    setCfg(c);
    set阈值输入(c.阈值秒);
    set口径输入(c.口径);
  }), []);

  useEffect(() => {
    刷配置().catch(() => {});
    刷账().catch((e) => set讯(String(e)));
    return () => 计时器.current && clearTimeout(计时器.current);
  }, [刷配置, 刷账]);

  const 包 = async (fn, 提示) => {
    set忙(true);
    set讯("");
    try {
      const x = await fn();
      if (提示) set讯(typeof 提示 === "function" ? 提示(x) : 提示);
      await 刷账();
      return x;
    } catch (e) {
      set讯("操作失败：" + String(e?.message || e));
    } finally {
      set忙(false);
    }
  };

  const 查 = (no) =>
    包(async () => {
      const id = no || 令号输入;
      if (!id) return;
      const v = await get("/超时查验?令号=" + encodeURIComponent(id));
      set查验(v);
      set令号输入(id);
      return v;
    }, (v) => (v ? `查验 ${v.令号} · ${v.状态} · valid=${v.valid}` : ""));

  const 存配置 = () =>
    包(() => post("/超时配置", { 阈值秒: Number(阈值输入), 口径: 口径输入 }),
      (x) => `配置已写入审计：阈值 ${x.阈值秒}s · 口径 ${x.口径}`).then(刷配置);

  const 预置 = (代号) =>
    包(() => post("/超时夹具", { 代号 }), (x) => {
      set设备键(x.设备键);
      set令号输入(x.令号);
      set查验(空查验);
      return `已预置${代号}：${x.令号}（在途+积压，阈值 ${x.阈值秒}s / ${x.口径}）`;
    }).then(() => 刷配置());

  const 拨并扫 = () =>
    包(async () => {
      await post("/拨超时", {});
      const x = await post("/超时扫描", {});
      if (令号输入) await 查(令号输入);
      return x;
    }, (x) => `扫描批次 ${x.批次}：候选 ${x.候选数} · 作废 ${x.作废数} · 新打戳 ${x.打戳数}`);

  const 重连 = () =>
    包(() => post("/设备册/重连补投", { 设备键 }), (x) => {
      if (令号输入) 查(令号输入);
      return `重连补投 ${x.补投数} 条${x.失败闭?.length ? ` · 失败闭 ${x.失败闭.length}` : ""}`;
    });

  const 一键对照 = () =>
    包(() => post("/演示/超时对照", {}), (x) => {
      set对照(x);
      return `对照成立：甲不送达=${x.甲不应再送达}，乙补投送达=${x.乙应补投送达}`;
    });

  const 压一次 = () =>
    包(() => post("/超时并发压测", { 单数: 8 }), (x) => {
      set压测(x);
      return x.通过 ? "并发交错压测通过：失败闭、无双发、无幽灵条" : "压测发现不一致，见下";
    });

  return (
    <div>
      <h2 className="题">在途超时作废</h2>
      <p className="述">
        只能钉死一条口径：<b>作废</b>（廊→作废、补投条→取消、重连不送达）或
        <b> 打戳</b>（廊留在途、写超时戳、重连按序补）。扫描与廊、条同票迁移；
        并发竞争失败闭；只做状态迁移，绝不无记录删令。
      </p>

      {/* 同源概览 */}
      <div className="带条" style={{ marginBottom: "1rem" }}>
        <div className="带项"><b>{源?.在途数 ?? "—"}</b><span>总览在途数（同源）</span></div>
        <div className="带项"><b>{账.在途数 ?? "—"}</b><span>廊在途 / 对账</span></div>
        <div className="带项"><b>{账.积压补投数 ?? "—"}</b><span>积压补投</span></div>
        <div className="带项"><b className="坏">{账.幽灵补投条数 ?? "—"}</b><span>幽灵补投条</span></div>
        <div className="带项">
          <b className={账.全部valid ? "好" : "坏"}>{String(!!账.全部valid)}</b>
          <span>对账全部 valid</span>
        </div>
      </div>

      {/* 配置 */}
      <section className="卡格">
        <div className="卡">
          <h3>① 超时口径与阈值（可配置 · 写审计）</h3>
          <div className="行">
            <label className="淡">阈值秒
              <input
                className="数入"
                type="number" min="0"
                value={阈值输入}
                onChange={(e) => set阈值输入(e.target.value)}
              />
            </label>
            <label className="淡">口径
              <select className="数入" value={口径输入} onChange={(e) => set口径输入(e.target.value)}>
                <option value="作废">作废（甲：不应再送达）</option>
                <option value="打戳">打戳（乙：仍补投送达）</option>
              </select>
            </label>
            <button className="主钮" disabled={忙} onClick={存配置}>写入配置</button>
            <span className="讯">当前 {cfg.阈值秒}s · {cfg.口径}</span>
          </div>
          <p className="淡 小注">
            扫描窗边界：状态='在途' 且 下发时 ≤ 扫描时刻−阈值（右界含，令龄≥阈值即命中）。
            作废说明带阈值快照。
          </p>
        </div>

        {/* 操作 */}
        <div className="卡">
          <h3>② 预置 → 拨超时 → 扫描 → 重连</h3>
          <div className="行">
            <button className="铜钮" disabled={忙} onClick={() => 预置("甲")}>预置甲（作废）</button>
            <button className="铜钮" disabled={忙} onClick={() => 预置("乙")}>预置乙（打戳）</button>
            <button className="主钮" disabled={忙} onClick={拨并扫}>拨超时并扫描</button>
          </div>
          <div className="行">
            <input className="文入" value={设备键} onChange={(e) => set设备键(e.target.value)} placeholder="设备键" />
            <button disabled={忙} onClick={() => 包(() => post("/设备册/断线", { 设备键 }), "已断线")}>断线</button>
            <button className="主钮" disabled={忙} onClick={重连}>重连补投</button>
            <button disabled={忙} onClick={() => 刷账().catch((e) => set讯(String(e)))}>刷新对账</button>
          </div>
          <div className="行">
            <button className="主钮" disabled={忙} onClick={一键对照}>一键甲乙对照演练</button>
            <button className="铜钮" disabled={忙} onClick={压一次}>并发交错压一次</button>
          </div>
          {讯 && <p className="讯" style={{ marginTop: 4 }}>{讯}</p>}
        </div>

        {/* 查验 */}
        <div className="卡">
          <h3>③ 查验（令号 / 状态 / 超时戳 / 补投条状态 / valid）</h3>
          <div className="行">
            <input className="文入" style={{ minWidth: 260 }} value={令号输入}
              onChange={(e) => set令号输入(e.target.value)} placeholder="CMD-..." />
            <button className="主钮" disabled={忙 || !令号输入} onClick={() => 查()}>查验</button>
          </div>
          <div className="验格">
            <div><span className="淡">令号</span><b>{查验.令号 || "—"}</b></div>
            <div><span className="淡">状态</span><b className={状态色(查验.状态)}>{查验.状态 ?? "—"}</b></div>
            <div><span className="淡">超时戳</span><b className="mono">{查验.超时戳 || "—"}</b></div>
            <div><span className="淡">补投条状态</span><b className={状态色(查验.补投条状态)}>{查验.补投条状态 ?? "—"}</b></div>
            <div><span className="淡">valid</span><徽章 ok={!!查验.valid} /></div>
          </div>
          {查验.说明 && <p className="淡 小注">{查验.说明}</p>}
          {Array.isArray(查验.问题) && 查验.问题.length > 0 && (
            <p className="坏 小注">问题：{查验.问题.join("；")}</p>
          )}
        </div>
      </section>

      {/* 同屏对账表：指令廊 × 补投条 */}
      <h3 className="题" style={{ fontSize: "1.05rem", marginTop: "1.2rem" }}>
        同屏对账 · 指令廊 × 断线补投条
      </h3>
      <table>
        <thead>
          <tr>
            <th>令号</th><th>设备</th><th>廊状态</th><th>超时戳</th>
            <th>补投条</th><th>valid</th><th>说明 / 问题</th>
          </tr>
        </thead>
        <tbody>
          {(账.行 || []).map((r) => (
            <tr key={r.令号} onClick={() => { set令号输入(r.令号); 查(r.令号); }} style={{ cursor: "pointer" }}>
              <td className="mono">{r.令号}</td>
              <td className="淡">{r.设备键}</td>
              <td className={状态色(r.廊状态)}>{r.廊状态}</td>
              <td className="淡 mono">{r.超时戳 || "—"}</td>
              <td className={状态色(r.补投条状态)}>{r.补投条状态}</td>
              <td><徽章 ok={r.valid} /></td>
              <td className="淡">
                {r.问题?.length ? <span className="坏">{r.问题.join("；")}</span> : (r.说明 || "—")}
              </td>
            </tr>
          ))}
          {(!账.行 || 账.行.length === 0) && (
            <tr><td colSpan={7} className="淡">暂无指令，先预置甲/乙或一键对照。</td></tr>
          )}
        </tbody>
      </table>

      {对照 && (
        <details className="卡" style={{ marginTop: "1rem" }} open>
          <summary><b>甲乙对照结果</b></summary>
          <div className="验格">
            <div><span className="淡">甲（作废）</span>
              <b>{对照.甲?.查验?.状态} / {对照.甲?.查验?.补投条状态} · 不送达={String(对照.甲不应再送达)}</b></div>
            <div><span className="淡">乙（打戳）</span>
              <b>{对照.乙?.查验?.状态} / {对照.乙?.查验?.补投条状态} · 送达={String(对照.乙应补投送达)}</b></div>
          </div>
        </details>
      )}

      {压测 && (
        <details className="卡" style={{ marginTop: "1rem" }} open>
          <summary><b>并发交错压测结果</b>
            <span className={压测.通过 ? "好" : "坏"}> {String(压测.通过)}</span>
          </summary>
          <p className="小注">
            作废口径 {压测.作废口径?.总数} 令：扫描胜 {压测.作废口径?.扫描作废胜} /
            补投胜 {压测.作废口径?.重连补投胜} / 双发 {压测.作废口径?.双发冲突数} /
            悬空 {压测.作废口径?.["悬空/不一致数"]}；
            打戳口径：送达 {压测.打戳口径?.补投送达数} / 留戳 {压测.打戳口径?.保留超时戳数}；
            幽灵条 {压测.对账?.幽灵补投条数}。
          </p>
          <pre className="预块">{JSON.stringify(压测, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
