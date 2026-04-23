import threading
import time
from pathlib import Path

import pytest

from src.tools.todo_tools import manage_todo, _todo_json_path, _todo_path


def test_atomic_save_and_read(tmp_path):
    workdir = tmp_path
    # Create a TODO list
    res = manage_todo(
        "create", str(workdir), steps=["a", "b", "c"], depends_on=[[], [0], [1]]
    )
    assert res["status"] == "ok"

    # Read it back
    r = manage_todo("read", str(workdir))
    assert r["status"] == "ok"
    assert r["total"] == 3

    # Check files exist
    assert _todo_json_path(str(workdir)).exists()
    assert _todo_path(str(workdir)).exists()


def test_concurrent_writes(tmp_path):
    workdir = tmp_path

    def writer(i):
        for _ in range(5):
            manage_todo(
                "create", str(workdir), steps=[f"task-{i}-{_}"], depends_on=[[]]
            )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Final read should succeed and produce a valid list
    r = manage_todo("read", str(workdir))
    assert r["status"] == "ok"
    # At least one list exists
    assert r["total"] >= 1


def test_create_rejects_out_of_range_dep(tmp_path):
    workdir = tmp_path
    # depends_on references index 5 which is out of range for 2 steps
    res = manage_todo("create", str(workdir), steps=["a", "b"], depends_on=[[], [5]])
    assert res["status"] == "error"
    assert "out-of-range" in res["error"] or "out of range" in res["error"]


def test_create_rejects_cycle(tmp_path):
    workdir = tmp_path
    # Simple 2-node cycle: 0 -> 1, 1 -> 0
    res = manage_todo("create", str(workdir), steps=["a", "b"], depends_on=[[1], [0]])
    assert res["status"] == "error"
    assert "cycle" in res["error"]


def test_threaded_concurrent_create_and_check(tmp_path):
    workdir = tmp_path

    # Start by creating an initial list of steps
    res = manage_todo(
        "create",
        str(workdir),
        steps=["s1", "s2", "s3", "s4"],
        depends_on=[[], [0], [1], [2]],
    )
    assert res["status"] == "ok"

    stop = False

    def creator():
        # Repeatedly create small lists
        for i in range(10):
            manage_todo(
                "create",
                str(workdir),
                steps=[f"c{i}-a", f"c{i}-b"],
                depends_on=[[], [0]],
            )

    def checker():
        # Repeatedly mark next available step as done if any
        for _ in range(20):
            cur = manage_todo("read", str(workdir))
            if cur["total"]:
                # try to check step 0 if exists
                try:
                    manage_todo("check", str(workdir), step_id=0)
                except Exception:
                    pass

    threads = []
    for _ in range(3):
        threads.append(threading.Thread(target=creator))
        threads.append(threading.Thread(target=checker))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Final read should succeed and produce a valid list structure
    r = manage_todo("read", str(workdir))
    assert r["status"] == "ok"
    assert isinstance(r.get("steps", []), list)
