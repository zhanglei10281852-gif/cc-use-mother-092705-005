# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 观鸟导赏报名服务

模块位于 `app/birding/`，为每周公众观鸟导赏提供场次、路线能力、同行关系与名额策略管理：

- **路线与场次**：路线有最大承载、难度和 `wheelchair_accessible` 无障碍标记；建场次时校验名额不得超过任何所选路线的承载，可单独设置每场每条路线的名额、无障碍配额和候补容量。
- **报名占位**：以整组（主报名人 + `companions`，`party_size` 必须一致）占用名额；需要无障碍的组只会被分配到无障碍路线并受无障碍配额约束；偏好路线优先，放不下自动改派其他开放路线，全部放不下则进入候补。同一场次同一联系人只能持有一条有效报名（部分唯一索引），重复请求靠 `idempotency_key` 幂等返回，不重复占位。
- **候补递补**：有人退出时在同一事务内严格按 `waitlist_rank` 顺序递补，队首整组放不下则停止，不跳过、不拆分；已签到（`checked_in`）和已取消的记录不会被递补覆盖，已签到记录不可取消。
- **路线保护关闭**：`POST /api/birding/events/{event_code}/routes/{route_code}/closure` 选择 `transfer`（整场转移到目标场次，含候补顺延）或 `cancel`（整场取消）。每位受影响的确认者、候补者都会收到通知；目标场次无满足承载/无障碍要求的名额时，该报名回退为取消并收到 `event_cancelled` 通知。
- **通知回执与轨迹**：通知有 `pending/delivered/failed` 状态与 `acknowledged_at` 回执时间；`GET /api/birding/events/{code}/timeline` 汇总报名、通知和操作日志，可观察候补晋升、通知回执和场次变更的完整轨迹。

### 端到端演示

```bash
python tools/birding_demo.py
```

脚本会用临时数据库创建三场不同条件的活动，依次演示报名/候补、重复请求幂等、退出递补、签到保护、通知投递与回执、整场转移和整场取消，并打印完整轨迹。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/birding/routes` | 创建路线（承载、难度、无障碍能力） |
| POST | `/api/birding/events` | 创建场次（路线集合、名额与候补策略） |
| GET | `/api/birding/events/{code}` | 场次状态与各路线占用 |
| POST | `/api/birding/registrations` | 报名（整组、幂等键、无障碍需求、偏好路线） |
| POST | `/api/birding/registrations/{id}/cancel` | 退出并触发按序递补 |
| POST | `/api/birding/registrations/{id}/check-in` | 现场签到 |
| POST | `/api/birding/events/{code}/routes/{route_code}/closure` | 路线关闭：整场转移或取消 |
| POST | `/api/birding/notifications/dispatch` | 投递待发通知（模拟短信网关回执） |
| POST | `/api/birding/notifications/{id}/ack` | 参与者确认收到通知（幂等） |
| GET | `/api/birding/events/{code}/timeline` | 报名、通知、操作日志完整轨迹 |
