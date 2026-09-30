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

`app/birding/` 提供每周公众观鸟导赏的场次报名能力，覆盖路线能力、同行关系、名额策略、候补递补与路线保护关闭处置。

### 业务规则

- **路线能力**：路线有保护承载量与无障碍标识；需要无障碍路线的报名不能落到无能力路线。
- **名额策略**：场次有效名额取「现场名额」与「路线承载」的较小值；报名按同行总人数（`party_size`，亲子同行可大于 1）计人头。
- **报名确认**：同一报名者在同一场次只允许一条生效中记录，重复占位返回 `409`；携带相同 `idempotency_key` 的重复请求原样返回既有记录，不产生第二条占位。
- **候补递补**：名额不足时按报名先后分配 `waitlist_seq`；有人退出后严格按候补序号递补，队首同行组放不下时不跳过其后的人，既定优先级不被破坏。
- **终态保护**：`checked_in`（已签到）与取消记录是终态，不能被取消或再次递补覆盖；重复签到、重复取消按幂等处理。
- **路线保护关闭**：路线关闭后关联未开场次冻结为 `closed_pending`，停止报名与递补；组织者必须对每场做「整场转移」或「整场取消」决定。
  - 转移前校验承接场次的路线承载、现场名额、无障碍能力以及报名者是否已在承接场次占位；
  - 取消/转移都会向每一位受影响者（含已签到者，不含此前已主动取消者）发出通知；
  - 通知有 `pending / acknowledged` 回执状态，可逐条确认（重复确认幂等）。

### 主要接口（前缀 `/api/birding`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/routes` `/routes/{code}/close` `/routes/{code}/reopen` | 路线与保护关闭/恢复 |
| POST/GET | `/sessions` `/sessions/{code}` | 创建、查询场次（含有效名额、已占、候补数） |
| POST | `/sessions/{code}/registrations` | 报名（确认或进入候补，支持幂等键） |
| GET | `/sessions/{code}/registrations` | 场次报名名单 |
| POST | `/registrations/{id}/cancel` `/registrations/{id}/check-in` | 退出（触发递补）/ 签到 |
| POST | `/sessions/{code}/closure-decision` | 路线关闭后的整场 `transfer` / `cancel` 决定 |
| GET | `/notifications` | 按场次、回执状态、报名者筛选通知 |
| POST | `/notifications/{id}/acknowledge` | 通知回执确认 |
| GET | `/sessions/{code}/timeline` | 决定、报名状态流转与通知回执的完整轨迹 |

### 端到端剧情演示

```bash
python -m app.cli birding-demo
```

该命令在独立数据库 `data/birding-demo.db` 上串联：多条件场次 → 亲子/无障碍报名 → 满员候补 → 幂等重放 → 签到 → 退出后按优先级递补 → 路线保护关闭 → 整场转移（承载与能力校验）→ 另一场整场取消 → 逐人通知回执 → 轨迹汇总，退出码 0 表示全部断言通过。

