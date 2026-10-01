#!/usr/bin/env python3
"""场馆疏散通道编排服务 —— Python 调用示例（仅依赖标准库）。

运行：  python3 examples/python_client.py [BASE_URL]
默认 BASE_URL=http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def show(title, status, payload):
    print(f"\n==== {title}（HTTP {status}）====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def summarize(plan):
    print(f"\n当前分钟: {plan['current_minute']}")
    for g in plan["groups"]:
        if g["status"] in ("scheduled", "fixed"):
            print(f"  组 {g['group_id']:>2} [{g['status']:>9}] "
                  f"出口 {g['exit_id']}  出发 {g['departure_minute']:>3} "
                  f"到达 {g['arrival_minute']:>3}  通道序列 {g['route']}")
        else:
            print(f"  组 {g['group_id']:>2} [{g['status']:>9}] "
                  f"原因 {g['reason']}：{g['detail']}")


if __name__ == "__main__":
    show("健康检查", *call("GET", "/health"))

    scenario = {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
            {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
            {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
            {"id": 4, "src": 1, "dst": 4, "minutes": 5, "capacity": 20},
            {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
            {"id": 6, "src": 4, "dst": 6, "minutes": 3, "capacity": 30},
            {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20},
        ],
        "exits": [5, 6],
        "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
        "groups": [
            {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 20, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 25},
            {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
             "priority": 20, "deadline": 20},
            {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0,
             "priority": 5, "deadline": 15},
        ],
    }
    status, plan = call("POST", "/scenario", scenario)
    show("初始化场景并编排", status, plan)
    summarize(plan)

    status, plan = call("POST", "/groups", [
        {"id": 9, "size": 40, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 12}])
    show("新增组 9（人数 40，截止很紧，演示无法安排的原因）", status, plan)
    summarize(plan)

    status, plan = call("POST", "/tick", {"minutes": 1})
    show("推进 1 分钟", status, plan)
    summarize(plan)

    # 组 3 在 t=0 进入通道 5（2->6），占用 [0,2)；当前分钟 1 时仍在途，
    # 与其剩余区间 [1,2) 相交的封闭应被整次拒绝
    status, payload = call("POST", "/closures",
                           {"edge_id": 5, "start_minute": 1, "end_minute": 3})
    show("触及已出发组剩余路线的封闭（预期 409）", status, payload)

    status, plan = call("GET", "/plan")
    show("拒绝后查询，原编排保持不变", status, plan)
    summarize(plan)

    # ---- 修订历史：追溯每次编排变更 ----
    status, payload = call("GET", "/revisions")
    show("修订列表（每次成功变更一条，拒绝不产生）", status, payload)

    latest = payload["revisions"][-1]["rev"]
    status, detail = call("GET", f"/revisions/{latest}")
    show(f"最新修订 rev={latest} 的快照与逐组差异", status, detail)
    for ch in detail["changes"]:
        if ch["change"] == "changed":
            print(f"  组 {ch['group_id']} 变化字段 {ch['changed_fields']}: "
                  f"{ch['before']['status']} -> {ch['after']['status']}")
        else:
            g = ch["after"] or ch["before"]
            print(f"  组 {ch['group_id']} {ch['change']} "
                  f"(status={g['status']})")

    status, payload = call("GET", "/revisions/999999")
    show("未知修订号（预期 404）", status, payload)
