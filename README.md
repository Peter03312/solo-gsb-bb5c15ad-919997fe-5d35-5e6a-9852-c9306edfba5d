# 场馆疏散通道编排服务

基于 **Python 3.11 / FastAPI / SQLite** 的疏散路线编排（scheduling）服务。
输入场馆的有向通道、封闭时段、出口与人群组，按优先级逐组安排**截止前最早到达**
的路线与出发时间，并保证通道每分钟容量不被超用、封闭时段不被占用。
支持推进时间、增量新增人群组/封闭，且只重排未出发组。

## 1. 一键启动

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后：

| 项目 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| SQLite 数据文件 | 容器内 `/data/evacuation.db`（命名卷 `evac_data` 持久化） |

首次启动且数据库为空时，会自动写入一份演示场馆数据并完成编排，直接
`curl http://localhost:8000/plan` 即可看到结果。

后台运行：`docker compose up --build -d`；停止：`docker compose down`
（保留数据）；清空数据重来：`docker compose down -v`。

### 本地（无 Docker）运行

```bash
pip install -r requirements.txt
EVAC_DB_PATH=./evacuation.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 2. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EVAC_DB_PATH` | `/data/evacuation.db` | SQLite 数据库文件路径，目录自动创建 |
| `EVAC_SEED` | `true` | 启动时若库为空，是否自动初始化演示数据（`true`/`false`） |

服务固定单 uvicorn worker 运行：所有编排变更由进程内 `asyncio` 锁串行化，
多副本水平扩展不在设计范围内（会绕过互斥锁）。

## 3. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查，返回当前分钟 |
| POST | `/scenario` | 全量重置场景（通道/出口/封闭/人群组），立即编排 |
| GET | `/plan` | 查询每组路线、出发/到达分钟或无法安排的原因 |
| POST | `/groups` | 新增一组或多组，**仅重排未出发组** |
| POST | `/closures` | 新增左闭右开封闭时段；触及已出发组剩余路线则 **409 整次拒绝** |
| POST | `/tick` | 推进当前分钟（body 可传 `{"minutes": n}`，默认 1） |
| GET | `/passages/{edge_id}/load` | 通道逐分钟占用，可加 `?minute=n` |
| GET | `/revisions` | 列出全部编排修订（修订号、触发操作、提交时分钟） |
| GET | `/revisions/{revision_id}` | 某次修订的前后编排快照与逐组差异；未知修订号 **404** |

### 请求体

**POST /scenario**

```json
{
  "passages": [
    {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30}
  ],
  "exits": [5, 6],
  "closures": [
    {"edge_id": 1, "start_minute": 3, "end_minute": 10}
  ],
  "groups": [
    {"id": 1, "size": 30, "origin": 1,
     "earliest_departure": 0, "priority": 10, "deadline": 30}
  ]
}
```

* `passages`：有向通道，`id` 唯一正整数；`minutes` 为正整数通行分钟数；
  `capacity` 为每分钟可同时容纳的人数（正整数）。
* `closures`：左闭右开 `[start_minute, end_minute)`。
* `groups`：整组不可拆分；`priority` 数值越大越优先。

**POST /groups**：请求体为上述 group 对象的数组。
**POST /closures**：单个 closure 对象。
**POST /tick**：`{"minutes": 3}` 或空 body（默认推进 1 分钟）。

### 响应（GET /plan 与所有变更接口）

```json
{
  "current_minute": 0,
  "groups": [
    {
      "group_id": 3,
      "status": "scheduled",
      "exit_id": 6,
      "departure_minute": 0,
      "arrival_minute": 2,
      "route": [5],
      "steps": [
        {"edge_id": 5, "from_node": 2, "to_node": 6,
         "enter_minute": 0, "exit_minute": 2}
      ],
      "reason": null,
      "detail": null
    }
  ]
}
```

组状态：`scheduled`（已安排未出发）、`fixed`（已出发，路线与占用固定）、
`failed`（无法在截止前安排）。

失败原因码：

| reason | 含义 |
| --- | --- |
| `BLOCKED_BY_CLOSURE` | 忽略封闭后可到达——封闭时段阻断了截止前所有路线 |
| `INSUFFICIENT_CAPACITY` | 忽略容量后可到达（或单组人数超通道容量）——容量竞争阻断 |
| `UNREACHABLE_EXIT` | 起点与任何出口之间不存在连通路径 |
| `DEADLINE_PASSED` | 当前分钟已超过该组规划截止分钟 |

封闭被拒绝时返回 HTTP 409：

```json
{
  "error": "CLOSURE_CONFLICTS_WITH_DEPARTED_GROUP",
  "message": "封闭时段与已出发组的剩余路线相交，整次拒绝",
  "conflicts": [
    {"group_id": 3, "edge_id": 5, "enter_minute": 0,
     "exit_minute": 2, "closure": [1, 3]}
  ]
}
```

## 4. 调用示例

```bash
# curl 版（依次演示初始化、新增组、新增封闭、推进分钟、409 拒绝、容量核验）
bash examples/curl_examples.sh

# Python 版（仅标准库）
python3 examples/python_client.py
```

最小示例：

```bash
curl -s http://localhost:8000/plan | python3 -m json.tool

curl -s -X POST http://localhost:8000/tick \
  -H 'Content-Type: application/json' -d '{"minutes": 5}'

# 追溯变更：列出全部修订，查看第 1 次修订的前后来龙去脉
curl -s http://localhost:8000/revisions | python3 -m json.tool
curl -s http://localhost:8000/revisions/1 | python3 -m json.tool
```

## 5. 编排语义（约定）

1. **时间**：当前分钟从 0 起按整数推进。组在第 `t` 分钟进入通行分钟数为
   `w` 的通道，占用分钟区间 `[t, t+w)`（左闭右开），第 `t+w` 分钟到达。
   到达分钟 `<= deadline` 即视为在截止前（第 `deadline` 分钟末）完成。
2. **封闭**：通行区间与任一封闭 `[a,b)` 相交（`t < b 且 a < t+w`）即不可
   进入；可在节点等待，封闭结束分钟 `b` 当分钟即可进入。
3. **容量**：同一通道同一分钟，所有在途组人数之和不得超过该通道容量。
4. **排序**（逐组贪婪）：优先级从高到低；同级按最早出发分钟升序；再相同
   按组编号升序。高优先级组先占容量，低优先级组只能使用剩余容量。
5. **择优**：在允许出发范围内取到达分钟最早；同到达取出口编号最小；
   再相同取通道编号序列字典序最小。算法为多标签时变 FIFO 最短路
   （等待只发生在节点，边到达时间关于进入时间单调，标签集合保证字典序
   并列情形正确）。
6. **推进与固定**：`/tick` 推进后，旧计划中出发分钟 `< 当前分钟` 的组
   立即视为已出发（`fixed`），路线与容量占用永久固定；其余未出发组
   （含此前失败的组）以当前分钟为最早出发时间整体重排。
7. **增量变更**：新增人群组或封闭时只重排未出发组；重排在单个 SQLite
   事务内“先整体删除旧路线、再写入新路线”，不会留下部分重排。
8. **封闭拒绝**：新封闭若与任一已出发组尚未走完的剩余路线相交，整次请求
   拒绝（409），不写入封闭、不触发重排、不改动任何编排。
9. **并发**：全部变更接口由同一把异步互斥锁串行化，容量逐组累加校验，
   并发变更不会超用容量。
10. **修订审计**：每次成功变更与一条修订记录（触发操作、提交时分钟、
    前后完整编排快照）在同一事务内提交；被拒绝的请求不产生修订；
    修订跨场景重置与服务重启保留，历史快照原样返回、不按当前场景重算。

## 6. 修订追溯（变更审计）

每次**成功**的编排变更——场景重置（`POST /scenario`）、新增人群组
（`POST /groups`）、登记封闭（`POST /closures`）、推进时间（`POST /tick`）
——都会生成一个**全局递增修订号**，并与编排变更在**同一个 SQLite 事务**
内提交（启动时自动写入的演示数据也会产生一条 `scenario` 修订）：

* 记录内容：触发操作（`scenario` / `groups` / `closures` / `tick`）、
  提交时的当前分钟、提交**前**与提交**后**的完整编排快照（与 `GET /plan`
  同构，含各组状态与逐段通行时刻）。
* 被拒绝的请求（封闭冲突 409、重复组 409、参数校验 422 等）不产生任何
  修订。
* 修订持久化在 SQLite 中：场景重置不会清除历史修订，服务重启后依然可查。
* 历史快照是变更提交时落库的结果（含当时 `fixed` 组状态与逐段
  enter/exit 分钟），查询历史时**原样返回、不按当前场景重算**。
* 并发变更由互斥锁串行化，修订号按实际提交顺序递增；编排变更与修订记录
  同事务提交，查询永远不会看到只写入其中一方的中间状态。

### 查询接口

`GET /revisions` —— 修订列表（按修订号升序）：

```json
{
  "revisions": [
    {"revision": 1, "operation": "scenario", "minute": 0},
    {"revision": 2, "operation": "tick", "minute": 5}
  ]
}
```

`GET /revisions/{revision_id}` —— 修订详情：前后快照 + 逐组差异：

```json
{
  "revision": 2,
  "operation": "tick",
  "minute": 5,
  "before": {"current_minute": 0, "groups": []},
  "after":  {"current_minute": 5, "groups": []},
  "changes": [
    {"group_id": 3, "change": "changed",
     "before": {}, "after": {},
     "fields": {"status": {"before": "scheduled", "after": "fixed"}}},
    {"group_id": 9, "change": "added",   "before": null, "after": {}},
    {"group_id": 7, "change": "removed", "before": {}, "after": null}
  ]
}
```

* `change`：`added`（本组新增）、`removed`（本组移除）、`changed`
  （字段变化，`fields` 给出逐字段前后值；比较字段为 `status`、`exit_id`、
  `departure_minute`、`arrival_minute`、`route`、`steps`、`reason`、
  `detail`）。
* 未变化的组不出现在 `changes` 中，其当时状态见 `before` / `after` 快照。
* 未知修订号返回 HTTP 404：

```json
{
  "error": "REVISION_NOT_FOUND",
  "message": "修订号 999 不存在，可先调用 GET /revisions 查看已有修订"
}
```

## 7. 项目结构

```
app/
  main.py       FastAPI 路由、事务/互斥、增量重排与拒绝逻辑、修订记录与查询
  planner.py    编排核心：时变 FIFO 多标签最短路 + 容量/封闭可行性
  db.py         SQLite 建表、世界状态加载、整组事务化持久化、修订读写
  schemas.py    Pydantic 输入/输出模型
  seed.py       启动演示数据
examples/       curl 与 Python 调用示例（含修订追溯）
tests/          算法单元测试与 API/并发/修订追溯集成测试（pytest）
```

本地运行测试：

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest -q
```
