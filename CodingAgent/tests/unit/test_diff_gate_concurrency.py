import threading
import random
import time


import src.tools._diff_gate as diff_gate


def test_register_resolve_concurrent():
    """Stress-test the diff-preview gate under concurrent register/resolve.

    Ensures no KeyError or deadlocks occur and that each registered Event is
    eventually set by a corresponding resolver.
    """
    # Ensure clean slate
    diff_gate.reset_preview_gate()

    N = 50
    keys = [f"concurrent-test-{i}" for i in range(N)]
    results: dict[str, bool] = {}

    def registrant(k: str) -> None:
        ev = diff_gate.register_preview_gate(k)
        # Wait up to 10s for resolver to arrive (should be fast in CI)
        res = ev.wait(timeout=10.0)
        results[k] = bool(res)

    reg_threads = [threading.Thread(target=registrant, args=(k,)) for k in keys]
    for t in reg_threads:
        t.start()

    # Small jitter so registers/ resolves overlap
    time.sleep(0.05)

    # Start resolvers in a shuffled order
    order = list(range(N))
    random.shuffle(order)

    def make_resolver(idx: int, k: str):
        def _resolver() -> None:
            # random tiny sleep to increase interleaving
            time.sleep(random.random() * 0.02)
            approved = idx % 2 == 0
            diff_gate.resolve_preview_gate(k, approved=approved)

        return _resolver

    res_threads = []
    for i in order:
        k = keys[i]
        t = threading.Thread(target=make_resolver(i, k))
        res_threads.append(t)
        t.start()

    # Join resolver threads
    for t in res_threads:
        t.join(timeout=5.0)

    # Join registrant threads
    for t in reg_threads:
        t.join(timeout=12.0)

    # All registrants should have observed their Event set
    assert len(results) == N, (
        f"Some registrant threads did not complete: {set(keys) - set(results.keys())}"
    )
    assert all(results.values()), f"Not all events were set: {results}"

    # After resolution, pending previews must be empty and rejected set size
    # should equal the number of rejects (odd indices)
    rejects_expected = sum(1 for i in range(N) if i % 2 == 1)
    assert not diff_gate.has_pending_previews(), "_pending_previews not empty"
    assert diff_gate.get_preview_rejected_count() == rejects_expected
