# 影湾 Shadowbay

系统与底层 · IoT 开发底座。

- 前端 http://127.0.0.1:7100
- 接口 http://127.0.0.1:12100

```powershell
docker compose up -d --build
```

本机开发：

```powershell
# 后端
cd backend
pip install -r requirements.txt
$env:预置目录 = (Resolve-Path ..\fixtures)
uvicorn app.main:app --reload --port 12100

# 前端
cd frontend
npm install
npm run dev
```

## 在途超时作废（超时扫描 × 断线补投同票）

超时口径持久化、可配置并写审计（`POST /超时配置`，键：`阈值秒`、`口径`）：

- **作废**（默认，对应夹具甲）：扫描命中后指令廊 `在途→作废`，同票把积压补投条 `积压→取消`；作废说明含超时与阈值快照。重连补投 CAS 失败闭，该令不再送达。
- **打戳**（对应夹具乙）：廊保持 `在途`、条保持 `积压`，仅写首次 `超时戳`；重连仍按序补投至 `送达/已补`，戳保留。

扫描窗边界：`状态='在途' AND 下发时 <= 扫描时刻-阈值`，右界含（令龄 ≥ 阈值即命中），批次号与左右界随响应返回。只做状态迁移，**绝不无记录删令**；廊/条迁移在同一 `BEGIN IMMEDIATE` 事务内并以 CAS 谓词 `状态='在途'` 定胜负，作废路径不留幽灵补投条。总览在途数与指令廊共用同一谓词计数，`GET /同源校验` 给单快照同源值。

常用路由（均有 `/api` 前缀镜像与英文别名）：

- `POST /超时扫描`（或裸 `POST /在途超时作废`）、`POST /拨超时`（虚拟时钟，免真等）
- `GET /超时查验?令号=…`、`GET /超时查验/{令号}` → `令号 / 状态 / 超时戳 / 补投条状态 / valid`
- `GET /对账`、`GET /同源校验`、`GET /总览`、`GET /指令廊`、`GET /补投条`
- `POST /超时夹具 {代号:"甲"|"乙", 执行到底:true}`、`POST /演示/超时对照`
- `POST /超时并发压测`：扫描与重连真实交错，断言无双发、无悬空、瞬时同源

自动化：

```bash
cd backend
pip install -r requirements-dev.txt
python3 -m pytest          # 含服务层并发与真实 HTTP（uvicorn 线程）交错用例
```

前端页面：`/超时作废`（同源计数、配置审计、甲乙夹具、查验五要素、同屏对账表、对照与压测面板）。

