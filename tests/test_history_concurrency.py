"""客户历史批次的并发幂等测试。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.history.service import submit
from app.history.store import HistoryStore


def test_parallel_submit_commits_single_batch(tmp_path, ready=None):
    from pathlib import Path
    data = (Path(__file__).resolve().parents[1] / "sample_data" / "history_ready.csv").read_bytes()
    store = HistoryStore(tmp_path / "history.db")
    barrier = Barrier(4)

    def load():
        barrier.wait(3)
        return submit(store, data)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: load(), range(4)))
    assert all(result["id"] == results[0]["id"] for result in results)
    assert len(store.list_batches()) == 1
    # 事件只登记一次
    batch = store.load_batch(results[0]["id"])
    assert len(batch["events"]) == 6
