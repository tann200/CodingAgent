import asyncio
import logging
from typing import Dict, Any, Optional, Tuple

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.tools.subagent_tools import delegate_task_async

logger = logging.getLogger(__name__)

# Role classifications for PRSW
READ_ONLY_ROLES = {"scout", "researcher", "reviewer"}
WRITE_ROLES = {"coder", "tester"}


async def _execute_delegation_with_locks(
    delegation: Dict,
    lock_manager,
    lock_type: str,
    state: AgentState,
    p2p_session=None,
    event_bus=None,
) -> Dict:
    """Execute a delegation with proper file locking (PRSW)."""
    role = delegation.get("role", "researcher")
    files = delegation.get("files", [])
    task = delegation.get("task", "")
    agent_id = delegation.get("agent_id", f"{role}_{id(delegation)}")

    acquired = []

    try:
        # Acquire locks based on type
        if lock_type == "read":
            for f in files:
                if await lock_manager.acquire_read_async(f, agent_id):
                    acquired.append(f)
        else:  # write
            for f in files:
                success = await lock_manager.acquire_write_async(
                    f, agent_id, timeout=30.0
                )
                if not success:
                    return {
                        "status": "error",
                        "error": f"Failed to acquire write lock for {f}",
                    }
                acquired.append(f)

        # HR-5 fix: enforce delegation depth limit to prevent recursive DoS.
        # CODINGAGENT_DELEGATION_DEPTH is injected into subagent env so child agents
        # inherit the depth counter without requiring state plumbing.
        import os

        current_depth = int(
            state.get("delegation_depth")
            or int(os.environ.get("CODINGAGENT_DELEGATION_DEPTH", "0"))
        )
        _MAX_DELEGATION_DEPTH = 3
        if current_depth >= _MAX_DELEGATION_DEPTH:
            logger.error(
                f"delegation_node: delegation depth {current_depth} >= {_MAX_DELEGATION_DEPTH}, "
                "refusing to spawn further subagent to prevent recursive DoS"
            )
            return {
                "status": "error",
                "error": f"delegation depth limit ({_MAX_DELEGATION_DEPTH}) reached",
            }
        os.environ["CODINGAGENT_DELEGATION_DEPTH"] = str(current_depth + 1)

        # HR-12 fix: wrap with timeout so a hung subagent does not block the parent.
        _DELEGATION_TIMEOUT_SECS = 300.0
        result = await asyncio.wait_for(
            delegate_task_async(
                role=role,
                subtask_description=task,
                working_dir=state.get("working_dir", ""),
            ),
            timeout=_DELEGATION_TIMEOUT_SECS,
        )

        # Publish to EventBus for P2P
        if event_bus:
            from src.core.orchestration.prsw_topics import AgentTopics, PRSWTopics

            if role == "scout":
                event_bus.publish(
                    AgentTopics.FILES_DISCOVERED,
                    {"files": files, "agent_id": agent_id, "result": result},
                )
            elif role == "researcher":
                event_bus.publish(
                    AgentTopics.DOC_SUMMARY,
                    {"summary": str(result)[:500], "agent_id": agent_id},
                )
            elif role == "reviewer":
                event_bus.publish(
                    AgentTopics.BUG_FOUND,
                    {"bugs": [], "agent_id": agent_id, "result": result},
                )

        return {
            "status": "completed",
            "result": result,
            "files": files,
            "role": role,
        }

    except asyncio.CancelledError:
        logger.warning(f"_execute_delegation_with_locks: cancelled for {agent_id}")
        raise

    except Exception as e:
        logger.error(f"_execute_delegation_with_locks: error for {agent_id}: {e}")
        return {"status": "error", "error": str(e)}

    finally:
        # Release locks
        for f in acquired:
            try:
                if lock_type == "read":
                    await lock_manager.release_read(f, agent_id)
                else:
                    await lock_manager.release_write(f, agent_id)
            except Exception as release_err:
                logger.error(f"Failed to release lock for {f}: {release_err}")

        if lock_type == "write" and lock_manager:
            lock_manager.reset_cancel()


async def delegation_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Delegation Layer: Spawns subagents for independent tasks that can run in parallel.

    This node enables:
    - Background memory operations (consolidation, skill learning)
    - Parallel code analysis
    - Independent verification tasks
    - Async subagent execution without blocking main pipeline

    Phase B: Publishes subagent results via P2P for cross-agent context sharing.
    Phase 6 (PRSW): Executes read agents in parallel, write agents sequentially.

    Subtasks are specified in state["delegations"] as a list of:
        {
            "role": "researcher|coder|reviewer|scout",
            "task": "description of subtask",
            "result_key": "key to store result under",
            "files": ["file1.py"]  # Optional: files this delegation will access
        }
    """
    logger.info("=== delegation_node START ===")

    delegations = state.get("delegations", [])
    if not delegations:
        logger.info("delegation_node: no delegations to process")
        return {}

    working_dir = state.get("working_dir", "")
    results: Dict[str, Any] = {"delegation_results": {}}

    # Phase B: Get P2P session manager for cross-agent context sharing
    session_id = state.get("session_id", "default")
    p2p_session = None
    try:
        from src.core.orchestration.agent_session_manager import (
            get_agent_session_manager,
        )

        # P1-1 fix: get_agent_session_manager() takes no arguments — it returns the
        # global singleton. session_id is used later when publishing, not for lookup.
        p2p_session = get_agent_session_manager()
    except Exception:
        logger.debug("delegation_node: P2P session not available")

    # Phase 6 (PRSW): Get file lock manager
    lock_manager = state.get("_file_lock_manager")
    event_bus = None
    orchestrator = None

    if lock_manager is None:
        try:
            orchestrator = _resolve_orchestrator(state, config)
            if orchestrator and hasattr(orchestrator, "file_lock_manager"):
                lock_manager = orchestrator.file_lock_manager
                event_bus = getattr(orchestrator, "event_bus", None)
        except Exception:
            logger.debug("delegation_node: could not get lock manager")

    # Determine if PRSW should be used (has files to lock)
    has_files = any(d.get("files") for d in delegations)
    use_prsw = has_files and lock_manager is not None

    if use_prsw:
        logger.info(f"delegation_node: using PRSW with {len(delegations)} delegations")

        # Separate delegations into READ and WRITE groups
        read_delegations = [
            d for d in delegations if d.get("role", "").lower() in READ_ONLY_ROLES
        ]
        write_delegations = [
            d for d in delegations if d.get("role", "").lower() in WRITE_ROLES
        ]

        # Phase 1: Execute READ agents in parallel
        read_results = {}
        if read_delegations:
            read_tasks = [
                _execute_delegation_with_locks(
                    d, lock_manager, "read", state, p2p_session, event_bus
                )
                for d in read_delegations
            ]
            read_results_list = await asyncio.gather(
                *read_tasks, return_exceptions=True
            )

            for i, result in enumerate(read_results_list):
                d = read_delegations[i]
                key = d.get("result_key", d.get("role", f"read_{i}"))

                if isinstance(result, Exception):
                    read_results[key] = {"status": "error", "error": str(result)}
                else:
                    read_results[key] = result

        # Phase 2: Execute WRITE agents sequentially
        write_results = {}
        for d in write_delegations:
            result = await _execute_delegation_with_locks(
                d, lock_manager, "write", state, p2p_session, event_bus
            )
            key = d.get("result_key", d.get("role", "write"))
            write_results[key] = result

        results["delegation_results"] = {**read_results, **write_results}
        results["_file_lock_manager"] = lock_manager
    else:
        # Original execution path (no PRSW)
        logger.info("delegation_node: using standard execution")

        async def run_delegation(
            delegation: Dict[str, Any], index: int
        ) -> Optional[Tuple[str, Any]]:
            role = delegation.get("role", "researcher")
            task = delegation.get("task", "")
            result_key = delegation.get("result_key") or f"delegation_{index}"

            if not task:
                return None

            logger.info(
                f"delegation_node: spawning {role} subagent for: {task[:50]}..."
            )

            try:
                # HR-12 fix: timeout so a hung subagent doesn't block the parent.
                result = await asyncio.wait_for(
                    delegate_task_async(
                        role=role,
                        subtask_description=task,
                        working_dir=working_dir,
                    ),
                    timeout=300.0,
                )
                logger.info(f"delegation_node: {result_key} completed")

                # Phase B: Publish result via P2P for cross-agent context (intra-session)
                if p2p_session:
                    try:
                        from src.core.orchestration.agent_session_manager import (
                            AgentRole,
                        )

                        # publish_to_role broadcasts to all agents subscribed to this role
                        p2p_session.publish_to_role(
                            sender_id=session_id,
                            role=AgentRole.ORCHESTRATOR,
                            payload={
                                "delegation_key": result_key,
                                "role": role,
                                "task": task,
                                "result": result,
                            },
                        )
                    except Exception as p2p_err:
                        logger.debug(f"P2P intra-session publish error: {p2p_err}")

                # Phase B (Step 5): Also publish via CrossSessionBus for inter-session routing.
                # This allows other concurrent sessions (scout, researcher, etc.) to receive
                # delegation results without going through the EventBus topic system.
                try:
                    from src.core.orchestration.cross_session_bus import (
                        get_cross_session_bus,
                    )
                    from src.core.orchestration.prsw_topics import AgentTopics

                    _topic = getattr(
                        AgentTopics,
                        f"{role.upper()}_RESULT",
                        AgentTopics.STATUS_UPDATE,
                    )
                    _topic_str = (
                        _topic.value if hasattr(_topic, "value") else str(_topic)
                    )
                    get_cross_session_bus().publish(
                        topic=_topic_str,
                        sender_session_id=session_id,
                        sender_role=role,
                        payload={
                            "delegation_key": result_key,
                            "role": role,
                            "task": task,
                            "result": result,
                        },
                    )
                except Exception as csb_err:
                    logger.debug(f"CrossSessionBus publish error: {csb_err}")

                return (result_key, {"status": "completed", "result": result})
            except Exception as e:
                logger.error(f"delegation_node: delegation failed: {e}")
                return (result_key, {"status": "error", "error": str(e)})

        # Standard execution path (non-PRSW)
        if len(delegations) == 1:
            result = await run_delegation(delegations[0], 0)
            if result is not None:
                result_key, value = result
                results["delegation_results"][result_key] = value
        else:
            tasks = [run_delegation(d, i) for i, d in enumerate(delegations)]
            delegation_results_list = await asyncio.gather(
                *tasks, return_exceptions=True
            )

            for result in delegation_results_list:
                if isinstance(result, Exception):
                    logger.error(f"delegation_node: exception in gather: {result}")
                    continue
                if result is None:
                    continue
                if not isinstance(result, tuple):
                    logger.error(
                        f"delegation_node: unexpected result type {type(result)}, skipping"
                    )
                    continue
                key, value = result
                results["delegation_results"][key] = value

    # C4 fix: inject completed delegation results into conversation history so the next
    # perception/planning cycle can see and use the subagent output.  Without this, results
    # were write-only — stored in state["delegation_results"] but never read by any node.
    delegation_history_msgs = []
    completed = results.get("delegation_results", {})
    if completed:
        summary_parts = []
        for key, val in completed.items():
            status = val.get("status", "unknown")
            if status == "completed":
                result_text = str(val.get("result", ""))[:500]
                summary_parts.append(f"**{key}**: {result_text}")
            else:
                summary_parts.append(
                    f"**{key}**: [error] {val.get('error', 'unknown error')}"
                )

        if summary_parts:
            delegation_summary = (
                "<delegation_results>\n"
                + "\n\n".join(summary_parts)
                + "\n</delegation_results>"
            )
            delegation_history_msgs.append(
                {"role": "user", "content": delegation_summary}
            )
            logger.info(
                f"delegation_node: injected {len(summary_parts)} result(s) into history"
            )

    logger.info("=== delegation_node END ===")
    return {**results, "history": delegation_history_msgs}


def create_delegation(
    role: str,
    task: str,
    result_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a delegation payload for the delegation node.

    Usage in other nodes (return from node, never mutate state directly):
        return {"delegations": [
            create_delegation("researcher", "Analyze the codebase structure"),
            create_delegation("reviewer", "Review the recent changes"),
        ]}
    """
    return {
        "role": role,
        "task": task,
        "result_key": result_key,
    }
