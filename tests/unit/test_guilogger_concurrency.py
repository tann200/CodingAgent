import threading
import time
from src.core.logger import logger as guilogger


def test_guilogger_concurrency():
    """Stress test GUILogger with multiple threads to ensure no exceptions and correct counts."""
    # reset logger state
    guilogger.clear()

    M = 10  # threads
    K = 50  # messages per thread -> total 500 < default max_history 1000

    def worker(tid):
        for j in range(K):
            guilogger.info(f"t{tid}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(M)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # give a tiny moment for any async ops (though log() is synchronous for history)
    time.sleep(0.1)

    logs = guilogger.get_logs(clear=False)
    # count messages emitted by our threads
    cnt = sum(1 for e in logs if isinstance(e.get("message"), str) and e["message"].startswith("t"))
    assert cnt >= M * K, f"expected at least {M*K} logs, got {cnt}"

