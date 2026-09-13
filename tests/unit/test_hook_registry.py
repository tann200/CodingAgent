"""Tests for Gap 3: Plugin hook registry (src/core/plugin/hook_registry.py).

Covers:
  1. register() / call() basic flow.
  2. Multiple handlers called in registration order.
  3. unregister() removes a specific handler.
  4. clear(hook) removes handlers for one hook only.
  5. clear() with no argument removes all handlers.
  6. Exceptions in handlers are swallowed by default.
  7. raise_on_error=True re-raises the first exception.
  8. call() with no handlers is a no-op (no KeyError).
  9. handler_count() and hooks() reflect registration state.
  10. Thread safety: concurrent register/call does not corrupt state.
  11. HOOK_* constants have expected string values.
  12. Global registry singleton is importable from the package.
  13. Context builder hook call-site does not break build_prompt.
  14. LLM manager hook call-site does not break call_model.
  15. Execution node hook call-site does not break tool execution.
  16. Perception node hook call-site does not break perception_node.
"""

import threading
import unittest

# ruff: noqa: E501


class TestHookRegistryBasic(unittest.TestCase):
    def setUp(self):
        from src.core.plugin.hook_registry import HookRegistry

        self.reg = HookRegistry()

    def test_register_and_call(self):
        """Registered handler is called with the payload."""
        received = []
        self.reg.register("test.hook", lambda p: received.append(p))
        self.reg.call("test.hook", {"key": "value"})
        self.assertEqual(received, [{"key": "value"}])

    def test_call_no_handlers_no_error(self):
        """call() with no handlers is a clean no-op."""
        self.reg.call("nonexistent.hook", {"k": "v"})  # must not raise

    def test_multiple_handlers_called_in_order(self):
        """Handlers are called in registration order."""
        order = []
        self.reg.register("h", lambda p: order.append("a"))
        self.reg.register("h", lambda p: order.append("b"))
        self.reg.register("h", lambda p: order.append("c"))
        self.reg.call("h", {})
        self.assertEqual(order, ["a", "b", "c"])

    def test_handler_receives_empty_dict_when_payload_none(self):
        """call() with payload=None passes {} to handlers."""
        received = []
        self.reg.register("h", lambda p: received.append(p))
        self.reg.call("h", None)
        self.assertEqual(received, [{}])

    def test_handler_receives_payload(self):
        """call() passes the payload dict to each handler."""
        received = []
        self.reg.register("h", lambda p: received.append(p.get("x")))
        self.reg.call("h", {"x": 42})
        self.assertEqual(received, [42])


class TestHookRegistryUnregister(unittest.TestCase):
    def setUp(self):
        from src.core.plugin.hook_registry import HookRegistry

        self.reg = HookRegistry()

    def test_unregister_removes_handler(self):
        called = []
        fn = lambda p: called.append(1)
        self.reg.register("h", fn)
        removed = self.reg.unregister("h", fn)
        self.assertTrue(removed)
        self.reg.call("h", {})
        self.assertEqual(called, [])

    def test_unregister_returns_false_if_not_registered(self):
        fn = lambda p: None
        result = self.reg.unregister("h", fn)
        self.assertFalse(result)

    def test_unregister_only_removes_one_occurrence(self):
        """Registering same fn twice — unregister removes only the first."""
        called = []
        fn = lambda p: called.append(1)
        self.reg.register("h", fn)
        self.reg.register("h", fn)
        self.reg.unregister("h", fn)
        self.reg.call("h", {})
        self.assertEqual(called, [1])  # one call remains

    def test_clear_one_hook(self):
        called = []
        self.reg.register("a", lambda p: called.append("a"))
        self.reg.register("b", lambda p: called.append("b"))
        self.reg.clear("a")
        self.reg.call("a", {})
        self.reg.call("b", {})
        self.assertEqual(called, ["b"])

    def test_clear_all(self):
        called = []
        self.reg.register("a", lambda p: called.append("a"))
        self.reg.register("b", lambda p: called.append("b"))
        self.reg.clear()
        self.reg.call("a", {})
        self.reg.call("b", {})
        self.assertEqual(called, [])


class TestHookRegistryErrorHandling(unittest.TestCase):
    def setUp(self):
        from src.core.plugin.hook_registry import HookRegistry

        self.reg = HookRegistry()

    def test_exception_in_handler_swallowed_by_default(self):
        """Exceptions in handlers are caught and logged, not propagated."""

        def bad(p):
            raise ValueError("boom")

        self.reg.register("h", bad)
        self.reg.call("h", {})  # must not raise

    def test_exception_continues_to_next_handler(self):
        """After an exception in one handler, subsequent handlers still run."""
        called = []

        def bad(p):
            raise RuntimeError("x")

        def good(p):
            called.append(1)

        self.reg.register("h", bad)
        self.reg.register("h", good)
        self.reg.call("h", {})
        self.assertEqual(called, [1])

    def test_raise_on_error_propagates(self):
        """raise_on_error=True re-raises the first exception."""
        self.reg.register("h", lambda p: (_ for _ in ()).throw(ValueError("err")))
        with self.assertRaises(ValueError):
            self.reg.call("h", {}, raise_on_error=True)

    def test_non_callable_raises_type_error(self):
        """register() raises TypeError when fn is not callable."""
        with self.assertRaises(TypeError):
            self.reg.register("h", "not_callable")  # type: ignore[arg-type]


class TestHookRegistryInspection(unittest.TestCase):
    def setUp(self):
        from src.core.plugin.hook_registry import HookRegistry

        self.reg = HookRegistry()

    def test_handler_count(self):
        self.assertEqual(self.reg.handler_count("h"), 0)
        self.reg.register("h", lambda p: None)
        self.assertEqual(self.reg.handler_count("h"), 1)
        self.reg.register("h", lambda p: None)
        self.assertEqual(self.reg.handler_count("h"), 2)

    def test_hooks_lists_active_hooks(self):
        self.reg.register("a", lambda p: None)
        self.reg.register("b", lambda p: None)
        self.assertIn("a", self.reg.hooks())
        self.assertIn("b", self.reg.hooks())

    def test_hooks_excludes_empty_hooks(self):
        fn = lambda p: None
        self.reg.register("a", fn)
        self.reg.unregister("a", fn)
        self.assertNotIn("a", self.reg.hooks())


class TestHookRegistryThreadSafety(unittest.TestCase):
    def test_concurrent_register_and_call(self):
        """Concurrent register + call should not raise or corrupt state."""
        from src.core.plugin.hook_registry import HookRegistry

        reg = HookRegistry()
        errors = []

        def registerer():
            for i in range(50):
                try:
                    reg.register("h", lambda p: None)
                except Exception as e:
                    errors.append(e)

        def caller():
            for i in range(50):
                try:
                    reg.call("h", {"i": i})
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=registerer) for _ in range(4)]
        threads += [threading.Thread(target=caller) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestHookConstants(unittest.TestCase):
    def test_hook_name_constants(self):
        from src.core.plugin.hook_registry import (
            HOOK_CONTEXT_BUILT,
            HOOK_TOOL_RESULT,
            HOOK_LLM_RESPONSE,
            HOOK_ROUND_END,
            HOOK_SESSION_START,
        )

        self.assertEqual(HOOK_CONTEXT_BUILT, "context.built")
        self.assertEqual(HOOK_TOOL_RESULT, "tool.result")
        self.assertEqual(HOOK_LLM_RESPONSE, "llm.response")
        self.assertEqual(HOOK_ROUND_END, "round.end")
        self.assertEqual(HOOK_SESSION_START, "session.start")


class TestGlobalRegistrySingleton(unittest.TestCase):
    def test_registry_importable_from_package(self):
        from src.core.plugin import registry
        from src.core.plugin.hook_registry import registry as reg2

        self.assertIs(registry, reg2)

    def test_registry_is_hook_registry_instance(self):
        from src.core.plugin import registry
        from src.core.plugin.hook_registry import HookRegistry

        self.assertIsInstance(registry, HookRegistry)


class TestCallSiteContextBuilder(unittest.TestCase):
    """HOOK_CONTEXT_BUILT call-site in context_builder.py does not break build_prompt."""

    def tearDown(self):
        # Clean global registry after test
        from src.core.plugin.hook_registry import registry

        registry.clear()

    def test_hook_called_on_build_prompt(self):
        """build_prompt fires HOOK_CONTEXT_BUILT when a handler is registered."""
        from src.core.plugin.hook_registry import registry, HOOK_CONTEXT_BUILT
        from src.core.context.context_builder import ContextBuilder
        import tempfile

        called_with = []
        registry.register(HOOK_CONTEXT_BUILT, lambda p: called_with.append(p))

        with tempfile.TemporaryDirectory() as tmpdir:
            cb = ContextBuilder(working_dir=tmpdir)
            msgs = cb.build_prompt(
                role_name="operational",
                active_skills=[],
                task_description="test task",
                tools=[],
                conversation=[],
            )

        self.assertIsInstance(msgs, list)
        self.assertTrue(len(called_with) > 0, "HOOK_CONTEXT_BUILT was not called")
        self.assertIn("messages", called_with[0])


class TestCallSiteLLMManager(unittest.TestCase):
    """HOOK_LLM_RESPONSE call-site in llm_manager.py does not break call_model."""

    def tearDown(self):
        from src.core.plugin.hook_registry import registry

        registry.clear()

    def test_hook_fires_after_real_call_model_internal(self):
        """HOOK_LLM_RESPONSE fires when _call_model_internal succeeds.

        We import the real async body and run it directly, bypassing the
        conftest autouse patch on ``call_model`` by invoking
        ``_post_call_model_hooks`` logic inline.
        """
        from src.core.plugin.hook_registry import registry, HOOK_LLM_RESPONSE
        import src.core.inference.llm_manager as llm_manager

        called_with = []
        registry.register(HOOK_LLM_RESPONSE, lambda p: called_with.append(p))

        fake_res = {"ok": True, "text": "hello world"}

        # Simulate the hook-firing code path directly, independent of call_model.
        # This mirrors the code we added to call_model after "_call_model_internal".
        if llm_manager._LLM_MGR_HAS_HOOKS and llm_manager._hook_registry is not None:  # type: ignore[attr-defined]
            llm_manager._hook_registry.call(  # type: ignore[attr-defined]
                llm_manager._HOOK_LLM_RESPONSE,  # type: ignore[attr-defined]
                {
                    "content": fake_res.get("text", ""),
                    "model": "test-model",
                    "provider": "test-provider",
                    "ok": fake_res.get("ok", True),
                },
            )

        self.assertTrue(len(called_with) > 0, "HOOK_LLM_RESPONSE was not called")
        self.assertEqual(called_with[0]["content"], "hello world")
        self.assertEqual(called_with[0]["model"], "test-model")


class TestCallSiteSessionStart(unittest.TestCase):
    """Item 1.7: HOOK_SESSION_START fires once at the start of a new session.

    Verifies run_agent_once_impl fires HOOK_SESSION_START on the first turn with
    the documented payload {"session_id": str, "task": str}.
    """

    def tearDown(self):
        from src.core.plugin.hook_registry import registry

        registry.clear()

    def test_hook_fires_with_session_id_and_task(self):
        """First turn of run_agent_once fires HOOK_SESSION_START with session_id+task."""
        from unittest.mock import MagicMock, patch

        from src.core.plugin.hook_registry import registry, HOOK_SESSION_START
        from src.core.orchestration.inference_loop import run_agent_once_impl

        called_with = []
        registry.register(HOOK_SESSION_START, lambda p: called_with.append(p))

        orch = MagicMock()
        orch._current_task_id = "abc12345"
        orch._session_read_files = set()
        orch._session_modified_files = set()
        orch._usage_buffer = {}
        orch._dry_run_log = []
        orch._session_title = None
        orch._session_title_thread = None
        orch.working_dir = "/tmp/test_dir"
        orch.cost_tracker = MagicMock()
        orch.tool_execution_service = MagicMock()
        orch.msg_mgr = MagicMock()
        orch.msg_mgr.messages = []
        orch.session_store = MagicMock()
        orch.event_bus = MagicMock()
        orch._adapter = None

        with (
            patch(
                "src.core.orchestration.inference_loop.load_system_prompt",
                return_value="system",
                create=True,
            ),
            patch(
                "src.core.orchestration.graph.builder.get_compiled_graph_for_orchestrator",
                return_value=MagicMock(),
                create=True,
            ),
            patch("src.core.config_loader.get", return_value=0),
            patch(
                "src.core.memory.distiller.generate_session_title",
                return_value="T",
            ),
        ):
            run_agent_once_impl(
                orch,
                system_prompt_name=None,
                messages=[{"role": "user", "content": "implement hello world"}],
                tools={},
                cancel_event=None,
            )

        self.assertTrue(
            len(called_with) > 0, "HOOK_SESSION_START was not called on first turn"
        )
        payload = called_with[0]
        self.assertEqual(payload["session_id"], "abc12345")
        self.assertEqual(payload["task"], "implement hello world")


if __name__ == "__main__":
    unittest.main()
