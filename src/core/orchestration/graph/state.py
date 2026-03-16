from typing import TypedDict, List, Dict, Any, Annotated, Optional
import operator

class AgentState(TypedDict):
    """
    Represents the shared state of the LangGraph cognitive pipeline.
    
    Fields:
        task: The primary goal or user prompt.
        history: The cumulative message history (appended automatically).
        verified_reads: programmatically tracked files that have been inspected.
        next_action: The tool call extracted from the LLM.
        last_result: The raw output of the last tool execution.
        rounds: Loop counter to prevent runaway execution.
        working_dir: Absolute path to the workspace sandbox.
        system_prompt: Base instructions loaded from agent-brain.
        errors: List of logic or system violations encountered.
    """
    task: str
    history: Annotated[List[Dict[str, Any]], operator.add]
    verified_reads: Annotated[List[str], operator.add]
    next_action: Optional[Dict[str, Any]]
    last_result: Optional[Dict[str, Any]]
    rounds: int
    working_dir: str
    system_prompt: str
    errors: List[str]
    # Planning and deterministic hints
    current_plan: Optional[List[Dict[str, Any]]]
    current_step: Optional[int]
    deterministic: Optional[bool]
    seed: Optional[int]
