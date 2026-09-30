"""场馆疏散通道编排服务 —— FastAPI 应用。

并发安全
========
所有会改变编排结果的请求（初始化场景 / 新增组 / 新增封闭 / 推进分钟）
由同一把 asyncio 锁串行化，且每个变更在单个 SQLite 事务内整体提交：
* 重排未出发组时，先删除这些组的全部旧路线再整体写入新路线，
  不会出现只重排了一部分的中间状态；
* 容量校验基于"已固定组占用 + 本轮已排入组占用"，锁内逐组累加，
  并发变更不可能超用容量；
* 新增封闭若与任一已出发（fixed）组剩余路线相交，直接返回 409，
  数据原样保留、不触发任何重排。
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db
from .planner import plan_group
from .schemas import (ClosureIn, ConflictOut, GroupIn, GroupPlanOut,
                      PlanResponse, ScenarioIn, StepOut)
from .seed import SEED_SCENARIO

DB_PATH = os.getenv("EVAC_DB_PATH", "/data/evacuation.db")
SEED_ON_START = os.getenv("EVAC_SEED", "true").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH, isolation_level=None)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await db.init_db(conn)
    app.state.db = conn
    app.state.lock = asyncio.Lock()

    async with app.state.lock:
        async with conn.execute("SELECT COUNT(*) FROM edges") as cur:
            edge_count = (await cur.fetchone())[0]
        if SEED_ON_START and edge_count == 0:
            await _load_scenario(
                conn, ScenarioIn.model_validate(SEED_SCENARIO))

    yield
    await conn.close()


app = FastAPI(
    title="场馆疏散通道编排服务",
    version="1.0.0",
    description="按优先级逐组编排截止前最早到达的疏散路线，"
                "支持封闭时段、每分钟容量约束与增量重排。",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# 内部编排逻辑（均在持锁状态下调用）
# --------------------------------------------------------------------------

async def _load_scenario(conn: aiosqlite.Connection, scenario: ScenarioIn) -> None:
    """全量写入一个场景，随后立即编排（整体为一个事务）。"""
    edge_ids = [p.id for p in scenario.passages]
    if len(edge_ids) != len(set(edge_ids)):
        raise HTTPException(422, "通道编号重复")
    group_ids = [g.id for g in scenario.groups]
    if len(group_ids) != len(set(group_ids)):
        raise HTTPException(422, "人群组编号重复")
    if len(scenario.exits) != len(set(scenario.exits)):
        raise HTTPException(422, "出口节点编号重复")

    nodes = {n for p in scenario.passages for n in (p.src, p.dst)}
    nodes.update(scenario.exits)
    for g in scenario.groups:
        nodes.add(g.origin)
    for c in scenario.closures:
        if c.edge_id not in edge_ids:
            raise HTTPException(422, f"封闭时段引用了不存在的通道 {c.edge_id}")
    for g in scenario.groups:
        if g.origin not in nodes:
            raise HTTPException(422, f"组 {g.id} 的起点 {g.origin} 不在场馆节点中")

    async with db.transaction(conn):
        await db.reset_db(conn)
        for p in scenario.passages:
            await conn.execute(
                "INSERT INTO edges(id, src, dst, minutes, capacity) VALUES(?,?,?,?,?)",
                (p.id, p.src, p.dst, p.minutes, p.capacity))
        for node in scenario.exits:
            await conn.execute(
                "INSERT INTO exits(node_id) VALUES(?)", (node,))
        for c in scenario.closures:
            await conn.execute(
                "INSERT INTO closures(edge_id, start_minute, end_minute) VALUES(?,?,?)",
                (c.edge_id, c.start_minute, c.end_minute))
        for g in scenario.groups:
            await conn.execute(
                """INSERT INTO groups
                   (id, size, origin, earliest_departure, priority, deadline, status)
                   VALUES(?,?,?,?,?,?,'pending')""",
                (g.id, g.size, g.origin, g.earliest_departure,
                 g.priority, g.deadline))
        await _replan_pending(conn, in_transaction=True)


async def _replan_pending(conn: aiosqlite.Connection,
                          in_transaction: bool = False) -> None:
    """重排全部未出发组（scheduled/failed/pending），整体替换。

    读阶段在事务外完成（SQLite autocommit 下读到的是最近一致快照），
    写阶段整体置于一个事务中，杜绝部分重排。
    """
    minute_now = await db.get_current_minute(conn)
    world, pending = await db.load_world(conn)

    node_set: set[int] = set()
    for e in world.edges.values():
        node_set.add(e.src)
        node_set.add(e.dst)
    node_set.update(world.exits)
    for g in pending:
        if g["origin"] not in node_set:
            raise HTTPException(422, f"组 {g['id']} 的起点 {g['origin']} 不连通")

    results = []
    # 已按 优先级 DESC, 最早出发 ASC, 组编号 ASC 排序
    for g in pending:
        result = plan_group(
            group_id=g["id"], size=g["size"], origin=g["origin"],
            earliest_departure=g["earliest_departure"],
            deadline=g["deadline"], current_minute=minute_now, world=world)
        results.append(result)
        if result["status"] == "scheduled":
            for (eid, minute), add in result["load_delta"].items():
                key = (eid, minute)
                world.fixed_load[key] = world.fixed_load.get(key, 0) + add

    if in_transaction:
        await db.persist_replan(conn, results, minute_now)
    else:
        async with db.transaction(conn):
            await db.persist_replan(conn, results, minute_now)


async def _closure_conflicts(conn: aiosqlite.Connection, edge_id: int,
                             a: int, b: int):
    """检查 [a,b) 是否触及已出发组在该通道上的剩余路线。"""
    minute_now, fixed_routes = await db.load_fixed_routes(conn)
    conflicts = []
    for gid, eid, enter_m, exit_m in fixed_routes:
        if eid != edge_id:
            continue
        remain_start = max(enter_m, minute_now)
        if remain_start >= exit_m:
            continue  # 该段已完全在过去，无剩余路线
        # 剩余区间 [remain_start, exit_m) 与封闭 [a,b) 相交
        if remain_start < b and a < exit_m:
            conflicts.append(ConflictOut(
                group_id=gid, edge_id=eid, enter_minute=enter_m,
                exit_minute=exit_m, closure=[a, b]))
    return conflicts


# --------------------------------------------------------------------------
# 响应组装
# --------------------------------------------------------------------------

async def _build_plan_response(conn: aiosqlite.Connection) -> PlanResponse:
    minute_now = await db.get_current_minute(conn)
    async with conn.execute(
            """SELECT id, status, departure_minute, arrival_minute, exit_id,
                      fail_reason, fail_detail
               FROM groups ORDER BY priority DESC,
                    earliest_departure ASC, id ASC""") as cur:
        group_rows = await cur.fetchall()
    out = []
    for gid, status, dep, arr, exit_id, reason, detail in group_rows:
        steps = []
        async with conn.execute(
                """SELECT edge_id, from_node, to_node, enter_minute, exit_minute
                   FROM route_steps WHERE group_id=? ORDER BY seq""", (gid,)) as sc:
            for row in await sc.fetchall():
                steps.append(StepOut(
                    edge_id=row[0], from_node=row[1], to_node=row[2],
                    enter_minute=row[3], exit_minute=row[4]))
        out.append(GroupPlanOut(
            group_id=gid, status=status, exit_id=exit_id,
            departure_minute=dep, arrival_minute=arr,
            route=[s.edge_id for s in steps], steps=steps,
            reason=reason, detail=detail))
    return PlanResponse(current_minute=minute_now, groups=out)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

@app.get("/health", tags=["运维"])
async def health(request: Request):
    return {"status": "ok",
            "current_minute": await db.get_current_minute(request.app.state.db)}


@app.post("/scenario", tags=["场景"], response_model=PlanResponse,
          status_code=200)
async def replace_scenario(scenario: ScenarioIn, request: Request):
    """全量重置场景（通道/出口/封闭/组），随后立即编排并返回结果。"""
    async with request.app.state.lock:
        conn = request.app.state.db
        await _load_scenario(conn, scenario)
        return await _build_plan_response(conn)


@app.post("/groups", tags=["变更"], response_model=PlanResponse,
          status_code=201)
async def add_groups(groups: list[GroupIn], request: Request):
    """新增一个或多个人群组；只对全部未出发组重新编排。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute("SELECT id FROM groups") as cur:
            existing = {row[0] for row in await cur.fetchall()}
        async with conn.execute(
                "SELECT src FROM edges UNION SELECT dst FROM edges") as cur:
            known_nodes = {row[0] for row in await cur.fetchall()}
        async with conn.execute("SELECT node_id FROM exits") as cur:
            known_nodes |= {row[0] for row in await cur.fetchall()}
        for g in groups:
            if g.id in existing:
                raise HTTPException(409, f"人群组编号 {g.id} 已存在")
            if g.origin not in known_nodes:
                raise HTTPException(422,
                    f"组 {g.id} 的起点 {g.origin} 不是场馆节点")
        async with db.transaction(conn):
            for g in groups:
                await conn.execute(
                    """INSERT INTO groups
                       (id, size, origin, earliest_departure, priority, deadline,
                        status)
                       VALUES(?,?,?,?,?,?,'pending')""",
                    (g.id, g.size, g.origin, g.earliest_departure,
                     g.priority, g.deadline))
            await _replan_pending(conn, in_transaction=True)
        return await _build_plan_response(conn)


@app.post("/closures", tags=["变更"], status_code=201)
async def add_closure(closure: ClosureIn, request: Request):
    """新增左闭右开封闭时段。

    若封闭触及任一已出发组尚未走完的路线，整次拒绝（409）且不改任何数据；
    否则登记封闭并重排全部未出发组。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute("SELECT 1 FROM edges WHERE id=?", (closure.edge_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(422, f"通道 {closure.edge_id} 不存在")

        conflicts = await _closure_conflicts(
            conn, closure.edge_id, closure.start_minute, closure.end_minute)
        if conflicts:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "CLOSURE_CONFLICTS_WITH_DEPARTED_GROUP",
                    "message": "封闭时段与已出发组的剩余路线相交，整次拒绝",
                    "conflicts": [cf.model_dump() for cf in conflicts],
                })

        async with db.transaction(conn):
            await conn.execute(
                "INSERT INTO closures(edge_id, start_minute, end_minute) VALUES(?,?,?)",
                (closure.edge_id, closure.start_minute, closure.end_minute))
            await _replan_pending(conn, in_transaction=True)
        return await _build_plan_response(conn)


class TickIn(BaseModel):
    minutes: int = Field(1, gt=0, description="推进的分钟数（正整数）")


@app.post("/tick", tags=["时间"], response_model=PlanResponse)
async def advance_minute(request: Request, payload: TickIn | None = None):
    """推进当前分钟（默认 +1，可传 {"minutes": n}）。

    推进时，旧计划中出发分钟已过（< 新当前分钟）的组视为已经出发，
    路线与占用立即固定、不参与重排；其余未出发组再按新当前分钟整体重排
    （新计划允许在当前分钟立即出发）。
    """
    conn = request.app.state.db
    delta = payload.minutes if payload is not None else 1

    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn) + delta
        async with db.transaction(conn):
            await db.set_current_minute(conn, minute_now)
            # 1) 旧计划中已经出发的组立即固定，不参与本次重排
            await conn.execute(
                """UPDATE groups SET status='fixed'
                   WHERE status='scheduled' AND departure_minute < ?""",
                (minute_now,))
            # 2) 其余未出发组以当前分钟为最早出发重新安排
            await _replan_pending(conn, in_transaction=True)
        return await _build_plan_response(conn)


@app.get("/plan", tags=["查询"], response_model=PlanResponse)
async def get_plan(request: Request):
    """查询当前编排：每组路线、出发/到达分钟或无法安排的原因。"""
    async with request.app.state.lock:
        return await _build_plan_response(request.app.state.db)


@app.get("/passages/{edge_id}/load", tags=["查询"])
async def get_edge_load(edge_id: int, request: Request,
                        minute: int | None = None):
    """查询通道逐分钟占用，便于核验容量；可加 ?minute=n 只看某一分钟。"""
    conn = request.app.state.db
    async with conn.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)) as cur:
        if await cur.fetchone() is None:
            raise HTTPException(404, f"通道 {edge_id} 不存在")
    async with request.app.state.lock:
        async with conn.execute(
                """SELECT rs.enter_minute, rs.exit_minute, g.size, g.status, g.id
                   FROM route_steps rs JOIN groups g ON g.id=rs.group_id
                   WHERE rs.edge_id=?""", (edge_id,)) as cur:
            rows = await cur.fetchall()
        per_minute: dict[int, dict] = {}
        for enter_m, exit_m, size, status, gid in rows:
            for m in range(enter_m, exit_m):
                if minute is not None and m != minute:
                    continue
                slot = per_minute.setdefault(
                    m, {"minute": m, "used": 0, "groups": []})
                slot["used"] += size
                slot["groups"].append(
                    {"group_id": gid, "size": size, "status": status})
        async with conn.execute(
                "SELECT capacity, minutes FROM edges WHERE id=?", (edge_id,)) as cur:
            cap_row = await cur.fetchone()
        return {"edge_id": edge_id, "capacity": cap_row[0],
                "traversal_minutes": cap_row[1],
                "minutes": sorted(per_minute.values(),
                                  key=lambda x: x["minute"])}
