import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parents[1]))

from src.core.orchestration.orchestrator import Orchestrator
from src.adapters.lm_studio_adapter import LmStudioAdapter

def dummy_read_file(path: str, **kwargs) -> str:
    return "Content of the file"

def dummy_edit_file(path: str, patch: str, **kwargs) -> str:
    return "File edited successfully"

def main():
    print("=== Testing Real LM Studio (Qwen 3.5 9B) ===")
    
    # 1. Setup real adapter pointed at localhost:1234
    adapter = LmStudioAdapter(
        base_url="http://localhost:1234/v1",
        models=["qwen/qwen3.5-9b"]
    )
    adapter.DEFAULT_TIMEOUT = 120.0  # 2 minutes to allow model to spin up and think
    
    # Check if we can reach it
    try:
        models = adapter.get_models_from_api()
        print(f"Connected to LM Studio. Available models: {models}")
    except Exception as e:
        print(f"Failed to connect to LM Studio: {e}")
        return

    # 2. Setup Orchestrator
    orch = Orchestrator(adapter=adapter, working_dir="/tmp/test_agent_dir")
    
    # Register our test tools
    orch.tool_registry.register("read_file", dummy_read_file, [], "Reads a file")
    orch.tool_registry.register("edit_file", dummy_edit_file, ["write"], "Edits a file with a patch")
    
    messages = [
        {"role": "user", "content": "Please read 'src/main.py' and tell me what you find. Just execute the tool call."}
    ]
    
    print("\n--- Running agent loop ---")
    res = orch.run_agent_once(system_prompt_name="operational", messages=messages, tools={})
    
    print("\n--- Final Messages ---")
    for m in orch.msg_mgr.all():
        role = m.get('role')
        content = m.get('content', '')
        # Print a snippet to avoid spamming console
        print(f"[{str(role).upper()}]: {content[:200]}...")
        if len(content) > 200:
            print("  ... (truncated)")
            
    print("\n--- Result object ---")
    print(res)

if __name__ == "__main__":
    main()
