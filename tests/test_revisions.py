"""修订追溯（变更审计）集成测试。

语义约定：
* 场景重置 / 新增组 / 登记封闭 / 推进时间成功时生成全局递增修订号，
  记录触发操作、提交时分钟与前后完整编排；
* 被拒绝的请求（409/422）不产生修订；
* 未知修订号返回 404 明确报错；
* 修订跨场景重置与服务重启保留；
* 历史快照保留当时 fixed 组状态与逐段通行时刻，原样返回、不重算；
* 编排变更与修订记录同事务提交，并发变更按实际提交顺序编号。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-revisions-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


BASE_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
        {"id": 2, "src": 2, "dst": 5, "minutes": 3, "capacity": 30},
    ],
    "exits": [5],
    "closures": [],
    "groups": [
        # 组 101：通道1+2，第 0 分钟出发，第 7 分钟到达
        {"id": 101, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        # 组 102：通道2，第 0 分钟出发，第 3 分钟到达
        {"id": 102, "size": 5, "origin": 2, "earliest_departure": 0,
         "priority": 5, "deadline": 30},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=BASE_SCENARIO)
        assert resp.status_code == 200
        yield c


def revisions(c):
    resp = c.get("/revisions")
    assert resp.status_code == 200
    return resp.json()["revisions"]


def last_revision(c):
    return revisions(c)[-1]


def get_revision(c, revision_id):
    resp = c.get(f"/revisions/{revision_id}")
    assert resp.status_code == 200
    return resp.json()


def changes_by_group(detail):
    return {c["group_id"]: c for c in detail["changes"]}


def test_scenario_reset_creates_revision_with_full_snapshots(client):
    new_scenario = {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 10},
        ],
        "exits": [2],
        "closures": [],
        "groups": [
            {"id": 201, "size": 4, "origin": 1, "earliest_departure": 0,
             "priority": 1, "deadline": 10},
        ],
    }
    resp = client.post("/scenario", json=new_scenario)
    assert resp.status_code == 200

    rev = last_revision(client)
    assert rev["operation"] == "scenario"
    assert rev["minute"] == 0

    detail = get_revision(client, rev["revision"])
    assert detail["revision"] == rev["revision"]
    assert detail["operation"] == "scenario"
    # 提交前快照是上一场景（fixture）的编排，提交后是新场景编排
    assert detail["before"]["current_minute"] == 0
    assert {g["group_id"] for g in detail["before"]["groups"]} == {101, 102}
    assert {g["group_id"] for g in detail["after"]["groups"]} == {201}
    assert detail["after"]["current_minute"] == 0
    # 逐组差异：旧组移除、新组新增
    changes = changes_by_group(detail)
    assert changes[101]["change"] == "removed"
    assert changes[101]["before"]["route"] == [1, 2]
    assert changes[101]["after"] is None
    assert changes[102]["change"] == "removed"
    assert changes[201]["change"] == "added"
    assert changes[201]["before"] is None
    assert changes[201]["after"]["status"] == "scheduled"
    assert changes[201]["after"]["route"] == [1]

    # 场景重置后旧修订依然可查（fixture 场景产生的修订）
    older = revisions(client)[-2]
    old_detail = get_revision(client, older["revision"])
    assert old_detail["operation"] == "scenario"
    assert {g["group_id"] for g in old_detail["after"]["groups"]} == {101, 102}


def test_add_groups_revision_records_added_group(client):
    resp = client.post("/groups", json=[
        {"id": 301, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 20, "deadline": 30}])
    assert resp.status_code == 201

    rev = last_revision(client)
    assert rev["operation"] == "groups"
    assert rev["minute"] == 0

    detail = get_revision(client, rev["revision"])
    changes = changes_by_group(detail)
    # 301 新增；通道1 容量 30 足够容纳 301 与 101（10+10），其余组计划不变
    assert set(changes) == {301}
    assert changes[301]["change"] == "added"
    assert changes[301]["after"]["route"] == [1, 2]
    assert changes[301]["after"]["departure_minute"] == 0
    # after 快照与当前 /plan 完全一致
    assert detail["after"] == client.get("/plan").json()


def test_closure_revision_shows_field_level_diff(client):
    # 封闭通道1 [0,6)：组101 推迟到第 6 分钟进入，第 13 分钟到达
    resp = client.post("/closures",
                       json={"edge_id": 1, "start_minute": 0, "end_minute": 6})
    assert resp.status_code == 201

    rev = last_revision(client)
    assert rev["operation"] == "closures"
    assert rev["minute"] == 0

    detail = get_revision(client, rev["revision"])
    changes = changes_by_group(detail)
    c101 = changes[101]
    assert c101["change"] == "changed"
    fields = c101["fields"]
    assert fields["departure_minute"] == {"before": 0, "after": 6}
    assert fields["arrival_minute"] == {"before": 7, "after": 13}
    assert "route" not in fields      # 通道序列未变
    assert "steps" in fields          # 逐段通行时刻后移
    # 组 102 不经过通道 1，计划不变，不出现在差异中
    assert 102 not in changes


def test_closure_revision_shows_failure_reason_diff(client):
    # 全时段封闭通道1：组101 唯一路线被阻断，转为 failed
    resp = client.post("/closures",
                       json={"edge_id": 1, "start_minute": 0, "end_minute": 40})
    assert resp.status_code == 201

    detail = get_revision(client, last_revision(client)["revision"])
    fields = changes_by_group(detail)[101]["fields"]
    assert fields["status"] == {"before": "scheduled", "after": "failed"}
    assert fields["reason"] == {"before": None, "after": "BLOCKED_BY_CLOSURE"}
    assert fields["departure_minute"] == {"before": 0, "after": None}
    assert fields["arrival_minute"] == {"before": 7, "after": None}


def test_tick_revision_fixes_departed_groups(client):
    resp = client.post("/tick", json={"minutes": 1})
    assert resp.status_code == 200

    rev = last_revision(client)
    assert rev["operation"] == "tick"
    assert rev["minute"] == 1

    detail = get_revision(client, rev["revision"])
    assert detail["before"]["current_minute"] == 0
    assert detail["after"]["current_minute"] == 1
    changes = changes_by_group(detail)
    # 两组均第 0 分钟出发，推进后固定
    assert changes[101]["fields"]["status"] == {"before": "scheduled",
                                                "after": "fixed"}
    assert changes[102]["fields"]["status"] == {"before": "scheduled",
                                                "after": "fixed"}
    # after 快照保留 fixed 组的逐段通行时刻
    g101 = {g["group_id"]: g for g in detail["after"]["groups"]}[101]
    assert g101["status"] == "fixed"
    assert g101["steps"][0] == {"edge_id": 1, "from_node": 1, "to_node": 2,
                                "enter_minute": 0, "exit_minute": 4}
    assert g101["steps"][1] == {"edge_id": 2, "from_node": 2, "to_node": 5,
                                "enter_minute": 4, "exit_minute": 7}


def test_rejected_requests_create_no_revision(client):
    count0 = len(revisions(client))

    # 重复组编号 → 409
    resp = client.post("/groups", json=[
        {"id": 101, "size": 1, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 5}])
    assert resp.status_code == 409
    # 封闭引用不存在的通道 → 422
    resp = client.post("/closures",
                       json={"edge_id": 999, "start_minute": 0, "end_minute": 1})
    assert resp.status_code == 422
    # 非法封闭时段（end <= start）→ 422
    resp = client.post("/closures",
                       json={"edge_id": 1, "start_minute": 3, "end_minute": 3})
    assert resp.status_code == 422
    assert len(revisions(client)) == count0

    # 触及已出发组剩余路线的封闭 → 409（先推进 1 分钟使组固定）
    resp = client.post("/tick", json={"minutes": 1})
    assert resp.status_code == 200
    count1 = len(revisions(client))
    assert count1 == count0 + 1  # tick 成功，产生一条修订
    resp = client.post("/closures",
                       json={"edge_id": 1, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 409
    assert len(revisions(client)) == count1  # 409 拒绝，不产生修订


def test_unknown_revision_returns_404(client):
    resp = client.get("/revisions/999999999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "REVISION_NOT_FOUND"
    assert "999999999" in body["message"]


def test_revision_list_is_ordered_with_operation_and_minute(client):
    client.post("/tick", json={"minutes": 2})
    revs = revisions(client)
    ids = [r["revision"] for r in revs]
    assert ids == sorted(ids)
    assert revs[-1] == {"revision": ids[-1], "operation": "tick", "minute": 2}


def test_historical_snapshot_preserved_not_recomputed(client):
    client.post("/tick", json={"minutes": 1})
    rev = last_revision(client)
    snapshot = get_revision(client, rev["revision"])
    g101 = {g["group_id"]: g for g in snapshot["after"]["groups"]}[101]
    assert g101["status"] == "fixed"
    assert g101["steps"][0]["enter_minute"] == 0

    # 场景重置：组 101/102 在当前场景中已不复存在
    resp = client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 2, "minutes": 1,
                      "capacity": 5}],
        "exits": [2], "closures": [], "groups": []})
    assert resp.status_code == 200
    assert client.get("/plan").json()["groups"] == []

    # 旧修订原样返回：fixed 状态与逐段时刻保留，不按当前场景重算
    assert get_revision(client, rev["revision"]) == snapshot


def test_revisions_survive_service_restart():
    with TestClient(app) as c1:
        assert c1.post("/scenario", json=BASE_SCENARIO).status_code == 200
        assert c1.post("/groups", json=[
            {"id": 401, "size": 5, "origin": 1, "earliest_departure": 0,
             "priority": 1, "deadline": 30}]).status_code == 201
        revs_before = revisions(c1)
        last = revs_before[-1]
        detail_before = get_revision(c1, last["revision"])

    # 模拟服务重启：重新进入 lifespan（新连接、同一 SQLite 文件）
    with TestClient(app) as c2:
        assert revisions(c2) == revs_before
        assert get_revision(c2, last["revision"]) == detail_before


def test_concurrent_changes_numbered_in_commit_order(client):
    base_count = len(revisions(client))

    payloads = [
        [{"id": 500 + i, "size": 5, "origin": 1, "earliest_departure": 0,
          "priority": 1, "deadline": 60}]
        for i in range(5)]

    def post(payload):
        return client.post("/groups", json=payload)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(post, payloads))
    assert all(r.status_code == 201 for r in responses)

    revs = revisions(client)
    new_revs = revs[base_count:]
    # 每次成功变更恰好一条修订，编号连续递增（按实际提交顺序）
    assert len(new_revs) == 5
    ids = [r["revision"] for r in new_revs]
    assert ids == list(range(ids[0], ids[0] + 5))
    assert all(r["operation"] == "groups" for r in new_revs)

    # 链式一致：上一修订的 after == 下一修订的 before，
    # 说明任何查询都看不到只写入一方的中间状态
    details = [get_revision(client, r["revision"]) for r in new_revs]
    for prev, nxt in zip(details, details[1:]):
        assert prev["after"] == nxt["before"]
    # 最后一版 after 与当前 /plan 一致
    assert details[-1]["after"] == client.get("/plan").json()
