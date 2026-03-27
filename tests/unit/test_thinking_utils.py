"""Tests for src.core.inference.thinking_utils."""
from src.core.inference.thinking_utils import (
    is_reasoning_model,
    supports_no_think,
    strip_thinking,
    budget_max_tokens,
)


class TestIsReasoningModel:
    def test_qwen3_detected(self):
        assert is_reasoning_model("qwen/qwen3.5-9b") is True

    def test_qwen3_upper(self):
        assert is_reasoning_model("Qwen3-8B-Instruct") is True

    def test_deepseek_r1_distill(self):
        assert is_reasoning_model("deepseek-ai/DeepSeek-R1-Distill-Llama-8B") is True

    def test_deepseek_r1_underscore(self):
        assert is_reasoning_model("deepseek_r1_distill_qwen_8b") is True

    def test_qwq(self):
        assert is_reasoning_model("qwq-32b-preview") is True

    def test_llama_not_reasoning(self):
        assert is_reasoning_model("meta-llama/Llama-3.1-8B-Instruct") is False

    def test_mistral_not_reasoning(self):
        assert is_reasoning_model("mistral-7b-instruct") is False

    def test_gemma_not_reasoning(self):
        assert is_reasoning_model("google/gemma-3-9b") is False

    def test_qwen2_not_reasoning(self):
        assert is_reasoning_model("qwen/qwen2.5-7b") is False

    def test_empty_string(self):
        assert is_reasoning_model("") is False


class TestSupportsNoThink:
    def test_qwen3_supports(self):
        assert supports_no_think("qwen/qwen3.5-9b") is True

    def test_qwq_supports(self):
        assert supports_no_think("qwq-32b") is True

    def test_deepseek_r1_does_not_support(self):
        assert supports_no_think("deepseek-r1-distill-llama-8b") is False

    def test_llama_does_not_support(self):
        assert supports_no_think("llama-3.1-8b") is False


class TestStripThinking:
    def test_strips_think_block(self):
        text = "<think>I need to think about this carefully.</think>\n\nFinal answer."
        assert strip_thinking(text) == "Final answer."

    def test_strips_multiline_think_block(self):
        text = "<think>\nLine 1\nLine 2\n</think>\n\n{\"key\": \"value\"}"
        assert strip_thinking(text) == '{"key": "value"}'

    def test_no_think_block_unchanged(self):
        text = '{"current_task": "list files", "next_step": "done"}'
        assert strip_thinking(text) == text

    def test_case_insensitive(self):
        text = "<THINK>reasoning</THINK>result"
        assert strip_thinking(text) == "result"

    def test_empty_string(self):
        assert strip_thinking("") == ""

    def test_multiple_think_blocks(self):
        text = "<think>first</think> middle <think>second</think> end"
        assert strip_thinking(text) == "middle  end"


class TestBudgetMaxTokens:
    def test_non_reasoning_model_unchanged(self):
        assert budget_max_tokens(400, "llama-3.1-8b") == 400

    def test_qwen3_unchanged_because_no_think_works(self):
        # Qwen3 supports /no_think so no budget doubling needed
        assert budget_max_tokens(400, "qwen/qwen3.5-9b") == 400

    def test_deepseek_r1_doubled(self):
        # DeepSeek-R1-Distill cannot suppress thinking → double the budget
        assert budget_max_tokens(400, "deepseek-r1-distill-llama-8b") == 800

    def test_deepseek_r1_qwen_doubled(self):
        assert budget_max_tokens(300, "deepseek-r1-distill-qwen-8b") == 600

    def test_empty_model_unchanged(self):
        assert budget_max_tokens(400, "") == 400

    def test_mistral_unchanged(self):
        assert budget_max_tokens(300, "mistral-7b-instruct") == 300
