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
    # Analysis phase output
    analysis_summary: Optional[str]
    relevant_files: Optional[List[str]]
    key_symbols: Optional[List[str]]
    # Debug retry tracking
    debug_attempts: Optional[int]
    max_debug_attempts: int
    # Verification result
    verification_passed: Optional[bool]
    verification_result: Optional[Dict[str, Any]]
    # Step controller
    step_controller_enabled: bool
    # Task decomposition
    task_decomposed: Optional[bool]
    # Tool cooldowns to prevent spam
    tool_last_used: Optional[Dict[str, int]]
    tool_call_count: int
    max_tool_calls: int
    # Files read tracking for read-before-edit
    files_read: Optional[Dict[str, bool]]
    # Repo summary data (automatically generated in analysis phase)
    repo_summary_data: Optional[str]
    # Patch size guard - replan required when patch exceeds 200 lines
    replan_required: Optional[str]
    action_failed: Optional[bool]
    # Plan progress tracking for UI dashboard
    plan_progress: Optional[Dict[str, Any]]
    # Evaluation result for task completion
    evaluation_result: Optional[str]
    # Cancel event for interrupting LLM generation
    cancel_event: Optional[Any]
    # Infinite loop prevention: track consecutive empty/no-tool responses
    empty_response_count: int
