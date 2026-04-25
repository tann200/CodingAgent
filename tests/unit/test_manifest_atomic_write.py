import threading
import time
import json
from json import JSONDecodeError
from pathlib import Path

from src.tools.subagent_tools import _atomic_write_json


def test_manifest_atomic_write_concurrent(tmp_path: Path):
    """Ensure _atomic_write_json never leaves a partially-written JSON file.

    We run a writer thread that repeatedly writes a moderately large JSON blob
    using the atomic writer while multiple reader threads concurrently try to
    open and json.load() the target file. Readers may see FileNotFoundError or
    valid JSON, but must never encounter JSONDecodeError.
    """

    target = tmp_path / "subagent_manifests" / "subagent_test.json"

    writer_cycles = 100
    writer_delay = 0.005
    # A reasonably large payload to increase IO time and contention window.
    large_payload = "x" * 100_000

    stop_event = threading.Event()

    json_errors = []
    other_exceptions = []

    def writer():
        try:
            for i in range(writer_cycles):
                obj = {"i": i, "data": large_payload}
                ok = _atomic_write_json(target, obj)
                assert ok, "atomic write reported failure"
                # Small pause to keep the writer active across several reader loops
                time.sleep(writer_delay)
        except Exception as e:  # pragma: no cover - surface unexpected failures
            other_exceptions.append(e)
        finally:
            stop_event.set()

    def reader():
        # Keep reading while the writer is active; tolerate FileNotFoundError
        while not stop_event.is_set():
            try:
                if not target.exists():
                    time.sleep(0.001)
                    continue
                with target.open("r", encoding="utf-8") as f:
                    json.load(f)
            except JSONDecodeError as jde:
                json_errors.append(jde)
                # brief backoff
                time.sleep(0.001)
            except FileNotFoundError:
                time.sleep(0.001)
            except Exception as e:  # pragma: no cover - surface unexpected failures
                other_exceptions.append(e)
                time.sleep(0.001)

    threads = []
    w = threading.Thread(target=writer, name="writer")
    w.start()
    threads.append(w)

    reader_count = 8
    for n in range(reader_count):
        t = threading.Thread(target=reader, name=f"reader-{n}")
        t.start()
        threads.append(t)

    # Wait for writer to finish (reasonable timeout)
    w.join(timeout=20)
    stop_event.set()

    # Join reader threads
    for t in threads:
        if t is w:
            continue
        t.join(timeout=1)

    assert not json_errors, f"Readers encountered JSONDecodeError(s): {json_errors}"
    assert not other_exceptions, f"Unexpected exceptions: {other_exceptions}"

    # Final file should be valid and contain the last writer payload
    final_text = target.read_text(encoding="utf-8")
    final = json.loads(final_text)
    assert "data" in final
    assert final["i"] == writer_cycles - 1
