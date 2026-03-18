import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from src.core.orchestration.graph.state import AgentState
from src.core.memory.distiller import distill_context
from src.core.memory.advanced_features import (
    TrajectoryLogger,
    DreamConsolidator,
    RefactoringAgent,
    ReviewAgent,
    SkillLearner,
)

logger = logging.getLogger(__name__)


async def memory_update_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Memory Update Layer: Persists distilled context and triggers advanced memory features.

    This node integrates:
    - distill_context: Updates TASK_STATE.md with task summary
    - TrajectoryLogger: Logs complete runs for training data
    - DreamConsolidator: Consolidates memories to prevent vector store growth
    - ReviewAgent: Reviews patches for issues
    - RefactoringAgent: Detects code smells in modified files
    - SkillLearner: Creates new skills from successful completions (future)
    """
    logger.info("=== memory_update_node START ===")

    orchestrator = config.get("configurable", {}).get("orchestrator")
    working_dir = state.get("working_dir", "unknown")
    workdir_path = Path(working_dir)

    history_len = len(state.get("history", []))
    logger.info(
        f"memory_update_node: processing {history_len} messages from {working_dir}"
    )

    try:
        # 1. Core: Trigger distillation to sync TASK_STATE.md
        distill_context(state["history"], working_dir=workdir_path)
        logger.info("memory_update_node: distillation complete")

    except Exception as e:
        logger.error(f"memory_update_node: distillation failed: {e}")

    # 2. Advanced Memory: Trajectory Logger
    # Log successful runs for training data
    try:
        evaluation_result = state.get("evaluation_result")
        task = state.get("task", "")
        current_plan = state.get("current_plan", [])
        history = state.get("history", [])
        tool_sequence = _extract_tool_sequence(history)

        # Determine if task was successful
        task_success = evaluation_result == "complete"

        if task_success and task:
            trajectory_logger = TrajectoryLogger(str(workdir_path))
            trajectory_logger.log_run(
                task=task,
                plan=str(current_plan),
                tool_sequence=tool_sequence,
                patch=_extract_patch_from_history(history),
                tests="",  # Would be populated from verification
                success=task_success,
                session_id=state.get("session_id"),
            )
            logger.info("memory_update_node: trajectory logged")
    except Exception as e:
        logger.warning(f"memory_update_node: trajectory logging failed: {e}")

    # 3. Advanced Memory: Dream Consolidator
    # Consolidate memories periodically
    try:
        consolidator = DreamConsolidator(str(workdir_path))
        consolidation_result = consolidator.consolidate_memories()
        logger.info(
            f"memory_update_node: consolidation complete: {consolidation_result.get('patterns', [])}"
        )
    except Exception as e:
        logger.warning(f"memory_update_node: consolidation failed: {e}")

    # 4. Advanced Memory: Review Agent
    # Review patches on successful completion
    try:
        if evaluation_result == "complete":
            history = state.get("history", [])
            patch = _extract_patch_from_history(history)

            if patch:
                review_agent = ReviewAgent(str(workdir_path))
                review_result = review_agent.review_patch(patch)
                logger.info(
                    f"memory_update_node: patch review complete: {review_result.get('overall', 'unknown')}"
                )

                # Store review in agent context for future reference
                review_path = workdir_path / ".agent-context" / "last_review.json"
                review_path.parent.mkdir(parents=True, exist_ok=True)
                import json

                review_path.write_text(json.dumps(review_result, indent=2))
    except Exception as e:
        logger.warning(f"memory_update_node: patch review failed: {e}")

    # 5. Advanced Memory: Refactoring Agent
    # Detect code smells in modified files
    try:
        if evaluation_result == "complete":
            history = state.get("history", [])
            modified_files = _extract_modified_files(history)

            refactoring_agent = RefactoringAgent(str(workdir_path))
            for file_path in modified_files:
                if file_path.endswith(".py"):
                    smells = refactoring_agent.detect_code_smells(file_path)
                    if smells:
                        logger.info(
                            f"memory_update_node: found {len(smells)} code smells in {file_path}"
                        )

                        # Store smells for reference
                        smells_path = (
                            workdir_path / ".agent-context" / "code_smells.json"
                        )
                        import json

                        existing_smells = {}
                        if smells_path.exists():
                            existing_smells = json.loads(smells_path.read_text())
                        existing_smells[file_path] = smells
                        smells_path.write_text(json.dumps(existing_smells, indent=2))
    except Exception as e:
        logger.warning(f"memory_update_node: refactoring analysis failed: {e}")

    # 6. Advanced Memory: Skill Learner
    # Extract a reusable skill pattern from a successful, non-trivial task
    try:
        task_success = evaluation_result == "complete"
        task = state.get("task", "")
        tool_sequence = _extract_tool_sequence(state.get("history", []))
        current_plan = state.get("current_plan", [])

        if task_success and task and len(tool_sequence) >= 2:
            skill_learner = SkillLearner(str(workdir_path))
            existing_skills = skill_learner.list_skills()

            # Derive a short skill name from the task (slug-ified)
            import re as _re

            skill_name = _re.sub(r"[^a-z0-9_]", "_", task[:40].lower()).strip("_")

            # Only create if a skill with this name doesn't already exist
            if skill_name and skill_name not in existing_skills:
                tool_names = [t.get("content", "")[:60] for t in tool_sequence[:5]]
                skill_learner.create_skill(
                    name=skill_name,
                    description=f"Auto-learned from successful task: {task[:120]}",
                    patterns=[f"Use {t}" for t in tool_names if t],
                    examples=[
                        {
                            "task": task[:200],
                            "solution": str(current_plan)[:400],
                        }
                    ],
                )
                logger.info(f"memory_update_node: created skill '{skill_name}'")
    except Exception as e:
        logger.warning(f"memory_update_node: skill learning failed: {e}")

    logger.info("=== memory_update_node END ===")
    return {}


def _extract_tool_sequence(history: List[Dict]) -> List[Dict]:
    """Extract tool calls from history."""
    tools = []
    for item in history:
        if isinstance(item, dict):
            role = item.get("role")
            content = item.get("content", "")
            if role == "tool" and content:
                tools.append({"content": content[:500]})  # Truncate for storage
    return tools


def _extract_patch_from_history(history: List[Dict]) -> str:
    """Extract patch from tool call history."""
    for item in reversed(history):
        if isinstance(item, dict):
            content = item.get("content", "")
            if "diff" in content.lower() or "patch" in content.lower():
                return content
            # Look for edit_file tool results
            if isinstance(content, str) and (
                "file" in content.lower() or "edited" in content.lower()
            ):
                return content
    return ""


def _extract_modified_files(history: List[Dict]) -> List[str]:
    """Extract list of modified files from history."""
    files = []
    for item in history:
        if isinstance(item, dict):
            content = str(item.get("content", ""))
            # Look for file paths in content
            if ".py" in content or ".js" in content or ".ts" in content:
                # Simple extraction - could be improved
                import re

                paths = re.findall(
                    r"(?:[\w\-/]+\.(?:py|js|ts|jsx|tsx|md|txt))", content
                )
                files.extend(paths)
    return list(set(files))[:10]  # Limit to 10 files
