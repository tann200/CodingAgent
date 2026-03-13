from typing import Any, Dict, List, Optional
from src.core.inference.llm_client import LLMClient
from src.core.inference.telemetry import with_telemetry

class DeterministicAdapter(LLMClient):
    def __init__(self, scenarios: Optional[Dict[str, List[str]]] = None):
        self.scenarios = scenarios or {}
        self.current_scenario = None
        self.step_index = 0
        self.name = "deterministic"
        self.provider = {"name": "deterministic", "type": "deterministic"}
        self.models = ["mock-model"]

    def set_scenario(self, scenario_name: str):
        self.current_scenario = scenario_name
        self.step_index = 0

    @with_telemetry
    def generate(self,
                 messages: List[Dict[str, str]],
                 model: Optional[str] = None,
                 stream: bool = False,
                 timeout: Optional[float] = None,
                 provider: Optional[str] = None,
                 **kwargs) -> Dict[str, Any]:
                 
        if not self.current_scenario or self.current_scenario not in self.scenarios:
            response_text = "No scenario configured."
        else:
            steps = self.scenarios[self.current_scenario]
            if self.step_index < len(steps):
                response_text = steps[self.step_index]
                self.step_index += 1
            else:
                response_text = "Scenario completed."
                
        return {
            "ok": True,
            "provider": "deterministic",
            "model": "mock-model",
            "prompt_tokens": 10,
            "completion_tokens": 10,
            "total_tokens": 20,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": response_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "raw": {"response": response_text}
        }
