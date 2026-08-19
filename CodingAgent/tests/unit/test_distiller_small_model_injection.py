import json
from typing import Any


def test_distiller_injects_small_model(monkeypatch):
    # Ensure get_small_model returns a known small model
    monkeypatch.setattr("src.core.config_loader.get_small_model", lambda: "tiny-model")

    recorded: dict[str, Any] = {"kwargs": None}

    def fake_call_model(*args, **kwargs):
        # record the kwargs used by _call_llm_sync
        recorded["kwargs"] = dict(kwargs)
        # Return a dict shaped like an LLM response containing JSON body
        body = json.dumps({"current_task": "t", "current_state": "s", "next_step": "n"})
        return {"choices": [{"message": {"content": body}}]}

    monkeypatch.setattr("src.core.inference.llm_manager.call_model", fake_call_model)

    from src.core.memory.distiller import distill_context

    msgs = [{"role": "user", "content": "Do something"}]
    res = distill_context(msgs, working_dir=None)

    # distillation should have succeeded and call_model should have been invoked
    assert isinstance(res, dict)
    assert recorded["kwargs"] is not None
    # The small-model injected by get_small_model should be present in kwargs
    assert recorded["kwargs"].get("model") == "tiny-model"
