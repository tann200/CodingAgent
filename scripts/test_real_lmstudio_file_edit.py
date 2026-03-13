import asyncio
import os
import sys
import json
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parents[1]))

from src.core.orchestration.orchestrator import Orchestrator
from src.adapters.lm_studio_adapter import LmStudioAdapter
from src.tools.file_tools import read_file, edit_file

def main():
    print("=== Testing Real LM Studio File Editing ===")
    
    adapter = LmStudioAdapter(
        base_url="http://localhost:1234/v1",
        models=["qwen/qwen3.5-9b"]
    )
    adapter.DEFAULT_TIMEOUT = 120.0
    
    try:
        models = adapter.get_models_from_api()
        print(f"Connected to LM Studio. Available models: {models}")
    except Exception as e:
        print(f"Failed to connect to LM Studio: {e}")
        return

    # Create real temp file
    work_dir = Path("/tmp/coding_agent_real_test")
    work_dir.mkdir(parents=True, exist_ok=True)
    target_file = work_dir / "target.txt"
    target_file.write_text("I love apples.\n")
    
    print(f"Created file at {target_file}")
    
    orch = Orchestrator(adapter=adapter, working_dir=str(work_dir))
    
    # Register real file tools
    orch.tool_registry.register("read_file", read_file, [], "Reads a file from the workspace")
    orch.tool_registry.register("edit_file", edit_file, ["write"], "Edits a file using patch format")
    
    messages = [
        {"role": "user", "content": "Read target.txt, then use edit_file to change 'apples' to 'oranges'. You must strictly use the edit_file tool."}
    ]
    
    print("\n--- Starting Agent Loop ---")
    
    # Run the loop for up to 5 steps
    for step in range(5):
        print(f"\nStep {step + 1}:")
        
        # In a real app, orchestrator handles the full history inside `msg_mgr`. 
        # But if we pass `messages` to run_agent_once, it appends it as user prompt. 
        # For the first step, pass the prompt. For subsequent steps, pass empty messages list.
        current_messages = messages if step == 0 else []
        
        res = orch.run_agent_once(system_prompt_name="operational", messages=current_messages, tools={})
        
        # Check if the orchestrator failed
        if "error" in res:
            print(f"Agent returned error: {res}")
            break
            
        last_msg = orch.msg_mgr.messages[-1]
        
        if last_msg["role"] == "user" and "tool_execution_result" in last_msg.get("content", ""):
            print(f"Tool executed. Result injected back to agent:")
            print(last_msg["content"][:200] + "...")
        else:
            print("No tool executed or loop finished.")
            # Check if file changed
            content = target_file.read_text()
            print(f"\nFinal File Content:\n{content}")
            if "oranges" in content:
                print("SUCCESS: The agent successfully edited the file!")
            else:
                print("FAILED: The agent did not edit the file.")
            break

if __name__ == "__main__":
    main()
