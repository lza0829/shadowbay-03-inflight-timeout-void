import React, { useEffect, useState } from "react";
import { get, post } from "../api.js";

const DEV = "dev-bay-01";

function 态色(s) {
  if (s === "已补" || s === "送达") return "好";
  if (s === "取消" || s === "作废") return "坏";
  if (s === "积压" || s === "在途") return "警";
  return "淡";
}

export default function 断线补投() {
  const [rows, setRows] = useState([]);
  const [msg, setMsg] = useState("");
  const 刷 = () => get("/补投条").then(setRows);
  useEffect(() => {
    刷();
  }, []);
  return (
    <div>
      <h2 className="题">断线补投</h2>
      <p className="述">
        离线时下发的指令按序积压；重连后逐条补投并闭环为送达。超时作废的令，其条同票改为
        「取消」且重连不再补；超时打戳的令仍积压待补。廊状态与超时戳同屏可对。
      </p>
      <div className="行">
        <button
          className="铜钮"
          onClick={() =>
            post("/设备册/断线", { 设备键: DEV })
              .then(() => setMsg("已断线"))
              .catch((e) => setMsg(String(e)))
          }
        >
          模拟断线
        </button>
        <button
          className="主钮"
          onClick={() =>
            post("/设备册/重连补投", { 设备键: DEV })
              .then((x) => {
                setMsg(`补投 ${x.补投数} 条`);
                刷();
              })
              .catch((e) => setMsg(String(e)))
          }
        >
          重连并补投
        </button>
        <button onClick={刷}>刷新</button>
        {msg && <span className="讯">{msg}</span>}
      </div>
      <table>
        <thead>
          <tr>
            <th>条号</th>
            <th>序</th>
            <th>令号</th>
            <th>本条状态</th>
            <th>廊状态</th>
            <th>超时戳</th>
            <th>入条</th>
            <th>终结/补投</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.条号}>
              <td className="mono">{r.条号}</td>
              <td>{r.序}</td>
              <td className="mono">{r.令号}</td>
              <td className={态色(r.状态)}>{r.状态}</td>
              <td className={态色(r.廊状态)}>{r.廊状态 || "—"}</td>
              <td className="淡 mono">{r.超时戳 || "—"}</td>
              <td className="淡">{r.入条时}</td>
              <td className="淡">{r.补投时 || r.取消时 || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
