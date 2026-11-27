import React, { useEffect, useState } from "react";
import { get, post } from "../api.js";

function 态色(s) {
  if (s === "拒令" || s === "作废" || s === "取消") return "坏";
  if (s === "送达" || s === "已补") return "好";
  if (s === "在途" || s === "积压") return "警";
  return "淡";
}

export default function 指令廊() {
  const [rows, setRows] = useState([]);
  const [last, setLast] = useState(null);
  const 刷 = () => get("/指令廊").then(setRows);
  useEffect(() => {
    刷();
  }, []);
  return (
    <div>
      <h2 className="题">指令廊</h2>
      <p className="述">
        在线直达为送达；离线入在途并进补投条；指纹不符即拒令。超时令按口径
        <b> 作废</b>（条同票取消）或<b> 打戳</b>（留在途待补）——与「断线补投」「超时作废」页同源。
      </p>
      <div className="行">
        <button
          className="主钮"
          onClick={() =>
            post("/演示/下发").then((x) => {
              setLast(x);
              刷();
            })
          }
        >
          推演示令
        </button>
        <button onClick={刷}>刷新</button>
        {last && (
          <span className="讯">
            最近 {last.令号} · {last.状态}
          </span>
        )}
      </div>
      <table>
        <thead>
          <tr>
            <th>令号</th>
            <th>设备</th>
            <th>动作</th>
            <th>状态</th>
            <th>超时戳</th>
            <th>补投条</th>
            <th>指纹</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.令号}>
              <td className="mono">{r.令号}</td>
              <td>{r.设备键}</td>
              <td>{r.动作}</td>
              <td className={态色(r.状态)}>{r.状态}</td>
              <td className="淡 mono">{r.超时戳 || "—"}</td>
              <td className={态色(r.补投条状态)}>{r.补投条状态 || "—"}</td>
              <td className="淡">{r.凭证指纹}</td>
              <td className="淡">{r.说明}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
