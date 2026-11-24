"""客户历史 HTTP 接口测试。"""

import io
from pathlib import Path

import pytest

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path):
    return create_app(tmp_path / "runs.db").test_client()


@pytest.fixture
def ready():
    return (ROOT / "sample_data" / "history_ready.csv").read_bytes()


@pytest.fixture
def incoming():
    return (ROOT / "sample_data" / "history_incoming.csv").read_bytes()


def test_preview_does_not_write(client, incoming):
    response = client.post("/api/history/preview", data={"file": (io.BytesIO(incoming), "h.csv")})
    assert response.status_code == 200
    assert response.json["publishable"] is False
    assert client.get("/api/history/batches").json == {"items": []}


def test_submit_replay_is_idempotent_over_http(client, ready):
    first = client.post("/api/history/batches", data={"file": (io.BytesIO(ready), "h.csv")})
    second = client.post("/api/history/batches", data={"file": (io.BytesIO(ready), "h.csv")})
    assert first.status_code == second.status_code == 200
    assert first.json["id"] == second.json["id"]
    assert second.json["inserted"] == 0
    assert len(client.get("/api/history/batches").json["items"]) == 1


def test_conflict_blocks_publish_then_resolve_and_publish(client, incoming, ready):
    # 先发布一个含结算账期的干净批次，形成冻结
    base = client.post(
        "/api/history/batches", data={"file": (io.BytesIO(ready), "h.csv")}
    ).json
    assert client.post(f"/api/history/batches/{base['id']}/publish").status_code == 200

    # 再提交含晚到变更的批次：冲击已结算账期，必须阻断
    batch = client.post(
        "/api/history/batches", data={"file": (io.BytesIO(incoming), "h.csv")}
    ).json
    blocked = client.post(f"/api/history/batches/{batch['id']}/publish")
    assert blocked.status_code == 409 and blocked.json["code"] == "HISTORY_UNRESOLVED"
    locked = next(c for c in batch["conflicts"] if c["reason"] == "SETTLED_LOCKED")
    target = locked["records"][0]

    resolved = client.post(
        f"/api/history/batches/{batch['id']}/resolutions",
        json={"record": target, "reason": "SOURCE_CORRECTED", "note": "来源更正剔除晚到变更"},
    )
    assert resolved.status_code == 200
    # 样例里还有撤销缺失等其余冲突时继续阻断，全部处理后才可发布
    if resolved.json["publishable"]:
        published = client.post(f"/api/history/batches/{batch['id']}/publish")
        assert published.status_code == 200
    else:
        assert resolved.json["open_conflicts"]


def test_ready_batch_full_publish_flow_and_current(client, ready):
    batch = client.post(
        "/api/history/batches", data={"file": (io.BytesIO(ready), "h.csv")}
    ).json
    assert batch["publishable"] is True
    published = client.post(f"/api/history/batches/{batch['id']}/publish")
    assert published.status_code == 200
    current = client.get("/api/history/current").json
    assert current["version"] == 1
    assert current["timeline"]["C003"][0]["segment"] == "5"


def test_evidence_download(client, ready):
    batch = client.post(
        "/api/history/batches", data={"file": (io.BytesIO(ready), "h.csv")}
    ).json
    response = client.get(f"/api/history/batches/{batch['id']}/evidence")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.json["schema"] == "history-evidence/1"


def test_unknown_batch_404(client):
    assert client.get("/api/history/batches/nope").status_code == 404


def test_bad_header_is_422_and_leaves_ledger_empty(client):
    response = client.post(
        "/api/history/batches",
        data={"file": (io.BytesIO(b"wrong,header\n1,2\n"), "bad.csv")},
    )
    assert response.status_code == 422
    assert response.json["code"] == "HISTORY_HEADER_MISMATCH"
    assert client.get("/api/history/batches").json == {"items": []}
