import threading
from src.core.observability.metrics import metrics


def test_counters_concurrent_writers() -> None:
    metrics.reset()

    def worker(n: int):
        for _ in range(n):
            metrics.increment_counter("concurrent.test", 1)

    threads = [threading.Thread(target=worker, args=(100,)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = dict(metrics.snapshot())
    counters = dict(snap.get("counters", {}))
    assert counters.get("concurrent.test") == 100 * 10


def test_histogram_record_and_reset() -> None:
    metrics.reset()
    for i in range(1, 51):
        metrics.record_histogram("h.test", float(i))
    snap = dict(metrics.snapshot())
    histograms = dict(snap.get("histograms", {}))
    hist = dict(histograms.get("h.test", {}))
    assert hist.get("count") == 50
    assert hist.get("min") == 1.0
    assert hist.get("max") == 50.0

    metrics.reset()
    snap2 = dict(metrics.snapshot())
    hist2 = dict(snap2.get("histograms", {}).get("h.test", {}))
    assert hist2.get("count", 0) == 0
