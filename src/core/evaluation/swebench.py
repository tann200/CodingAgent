"""SWE-bench integration harness (audit PHASE-3 item 3.3).

Loads SWE-bench style instances (``instance_id``, ``repo``, ``base_commit``,
``problem_statement``, ``patch``, ``test_patch``, ``FAIL_TO_PASS``,
``PASS_TO_PASS``), maps each to an evaluation candidate, and grades a run by:

1. extracting the agent's patch from the run working tree (``git add -N`` +
   ``git diff`` so new files are captured),
2. applying the instance ``test_patch``,
3. running the FAIL_TO_PASS/PASS_TO_PASS tests (or an explicit
   ``test_command``) and requiring a clean result.

Network use is limited to explicit live runs: ``repo`` may be a local path
(offline fixtures / tests), a ``file://`` URL, or an ``org/repo`` GitHub spec
(requires network at grading time).  Tests only use local fixture repos.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SWEBENCH_REQUIRED_FIELDS = (
    "instance_id",
    "problem_statement",
    "repo",
    "base_commit",
)


@dataclass
class SWEBenchInstance:
    """A single SWE-bench style problem instance."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: Optional[str] = None
    test_patch: Optional[str] = None
    fail_to_pass: List[str] = field(default_factory=list)
    pass_to_pass: List[str] = field(default_factory=list)
    test_command: Optional[str] = None
    environment_setup_commit: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SWEBenchInstance":
        for key in SWEBENCH_REQUIRED_FIELDS:
            value = data.get(key)
            if value is None or str(value) == "":
                raise ValueError(f"SWE-bench instance missing required field {key!r}")
        return cls(
            instance_id=str(data["instance_id"]),
            repo=str(data["repo"]),
            base_commit=str(data["base_commit"]),
            problem_statement=str(data["problem_statement"]),
            patch=data.get("patch"),
            test_patch=data.get("test_patch"),
            fail_to_pass=list(data.get("fail_to_pass") or data.get("FAIL_TO_PASS") or []),
            pass_to_pass=list(data.get("pass_to_pass") or data.get("PASS_TO_PASS") or []),
            test_command=data.get("test_command"),
            environment_setup_commit=data.get("environment_setup_commit"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "patch": self.patch,
            "test_patch": self.test_patch,
            "fail_to_pass": self.fail_to_pass,
            "pass_to_pass": self.pass_to_pass,
            "test_command": self.test_command,
            "environment_setup_commit": self.environment_setup_commit,
        }


def _parse_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def load_instances(source: Any) -> List[SWEBenchInstance]:
    """Load SWE-bench instances from a JSON file, a JSONL file, or a directory.

    Accepted documents:
    - ``.json`` holding a ``list`` of records, a dict keyed by instance_id, or
      a single record.
    - ``.jsonl`` with one record per line.
    - a directory scanned for ``*.json`` / ``*.jsonl`` files.

    Raises: ``ValueError`` on unreadable/unparseable input or missing fields.
    """
    path = Path(source)
    if path.is_dir():
        candidates = sorted(list(path.glob("*.json")) + list(path.glob("*.jsonl")))
        if not candidates:
            raise ValueError(f"no SWE-bench instance files found in {path}")
        instances: List[SWEBenchInstance] = []
        for candidate in candidates:
            instances.extend(load_instances(candidate))
        return instances

    if not path.exists():
        raise ValueError(f"SWE-bench instance source not found: {path}")

    if path.suffix == ".jsonl":
        data: Any = _parse_jsonl(path)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        if "instance_id" in data:
            data = [data]
        else:
            data = list(data.values())

    if not isinstance(data, list):
        raise ValueError(f"invalid SWE-bench instance document: {path}")

    return [SWEBenchInstance.from_dict(record) for record in data]


def _is_local_repo_spec(repo: str) -> bool:
    if repo.startswith(("file://", "./", "../", "/")):
        return True
    if Path(repo).exists():
        return True
    return False


def checkout_repo(instance: SWEBenchInstance, dest: Path) -> Path:
    """Clone/checkout the instance repo at ``base_commit`` into ``dest``.

    Uses a local copy for local paths / ``file://`` URLs so offline fixtures
    and tests never touch the network.  For ``org/repo`` GitHub specs a live
    clone is attempted (network required).
    """
    dest.mkdir(parents=True, exist_ok=True)
    if _is_local_repo_spec(instance.repo):
        clone_url = instance.repo
        if instance.repo.startswith("file://"):
            clone_url = instance.repo[len("file://"):]
    else:
        clone_url = f"https://github.com/{instance.repo}"
    repo_dir = dest / "repo"
    subprocess.run(
        ["git", "clone", "--quiet", str(clone_url), str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(dest),
    )
    subprocess.run(
        ["git", "checkout", "--quiet", instance.base_commit],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    return repo_dir


@dataclass
class GradeResult:
    """Grading outcome for one SWE-bench instance run."""

    status: str  # "pass" | "fail" | "error"
    patch: Optional[str] = None
    output: str = ""
    fail_to_pass_passed: int = 0
    pass_to_pass_passed: int = 0
    error: Optional[str] = None


def _extract_patch(repo_dir: Path) -> Tuple[str, Optional[str]]:
    """Return ``(patch, error)`` for the agent's working-tree changes."""
    staged = subprocess.run(
        ["git", "add", "-N", "."], capture_output=True, text=True, cwd=str(repo_dir)
    )
    if staged.returncode != 0:
        return "", staged.stderr
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
    )
    if diff.returncode != 0:
        return "", diff.stderr
    return diff.stdout, None


def _apply_test_patch(repo_dir: Path, test_patch: Optional[str]) -> Optional[str]:
    """Apply the instance ``test_patch``; return an error message or None."""
    if not test_patch or not test_patch.strip():
        return None
    patch_file = repo_dir / "_swebench_test_patch.diff"
    patch_file.write_text(test_patch, encoding="utf-8")
    try:
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file.name)],
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return exc.stderr or str(exc)
    finally:
        patch_file.unlink(missing_ok=True)
    return None


def _run_tests(repo_dir: Path, instance: SWEBenchInstance, timeout: int) -> Tuple[int, str]:
    """Run the instance tests; returns ``(returncode, output)``."""
    if instance.test_command:
        cmd = shlex.split(instance.test_command)
    else:
        test_ids = list(dict.fromkeys(instance.fail_to_pass + instance.pass_to_pass))
        cmd = ["python", "-m", "pytest", "-q", "--no-header", "--tb=short", "-x"]
        cmd.extend(test_ids)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
            timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return -1, "test command timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return -1, f"test command error: {exc}"


def grade_runnable(
    repo_dir: Path,
    instance: SWEBenchInstance,
    *,
    timeout: int = 300,
) -> GradeResult:
    """Grade an agent run that completed in ``repo_dir``.

    ``repo_dir`` must be a git checkout of the repo at ``base_commit`` with the
    agent's changes in the working tree (no commit required).  Returns a
    ``GradeResult`` with status pass/fail/error.
    """
    patch, patch_err = _extract_patch(repo_dir)
    if patch_err:
        return GradeResult(status="error", error=f"patch extraction failed: {patch_err}")
    if not patch.strip():
        return GradeResult(
            status="fail", patch=patch, error="no changes produced by the agent"
        )

    apply_err = _apply_test_patch(repo_dir, instance.test_patch)
    if apply_err:
        return GradeResult(
            status="error", patch=patch, error=f"test_patch apply failed: {apply_err}"
        )

    returncode, output = _run_tests(repo_dir, instance, timeout=timeout)
    if returncode == -1:
        return GradeResult(status="error", patch=patch, output=output, error=output)

    passed = returncode == 0
    if passed:
        f2p_ok, p2p_ok = len(instance.fail_to_pass), len(instance.pass_to_pass)
    else:
        fail_lines = {
            line.split(" ", 1)[0].strip() for line in output.splitlines()
            if "FAILED" in line or "ERROR" in line
        }
        f2p_ok = sum(1 for t in instance.fail_to_pass if t not in fail_lines)
        p2p_ok = sum(1 for t in instance.pass_to_pass if t not in fail_lines)
    return GradeResult(
        status="pass" if passed else "fail",
        patch=patch,
        output=output,
        fail_to_pass_passed=f2p_ok,
        pass_to_pass_passed=p2p_ok,
    )


def _now_iso() -> str:
    return datetime.now().isoformat()


class SWEBenchRunner:
    """Execute SWE-bench instances against an agent factory, producing records
    compatible with the shared evaluation CLI / regression baselines."""

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp())
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run_instance(
        self,
        instance: SWEBenchInstance,
        agent_factory: Any,
    ) -> Dict[str, Any]:
        start_iso = _now_iso()
        run_dir = self.workdir / instance.instance_id
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        start = datetime.fromisoformat(start_iso)

        try:
            repo_dir = checkout_repo(instance, run_dir)
        except Exception as exc:
            return self._result(
                instance, start, status="error", error=f"repo checkout failed: {exc}"
            )

        try:
            agent = agent_factory()
            _ensure_working_dir(agent, repo_dir)
            _invoke_swebench_agent(agent, instance, repo_dir)
        except Exception as exc:
            logger.warning(
                "SWEBenchRunner: agent raised for %s: %s", instance.instance_id, exc
            )

        grade = grade_runnable(repo_dir, instance)
        result = self._result(
            instance,
            start,
            status=grade.status,
            error=grade.error,
            verification_output=grade.output,
        )
        result["patch"] = grade.patch
        result["fail_to_pass_passed"] = grade.fail_to_pass_passed
        result["pass_to_pass_passed"] = grade.pass_to_pass_passed
        return result

    @staticmethod
    def _result(
        instance: SWEBenchInstance,
        start: datetime,
        *,
        status: str,
        error: Optional[str] = None,
        verification_output: str = "",
    ) -> Dict[str, Any]:
        end = datetime.now()
        return {
            "scenario_name": instance.instance_id,
            "status": status,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "duration_seconds": (end - start).total_seconds(),
            "error": error,
            "verification_output": verification_output,
            "patch": None,
            "fail_to_pass_passed": 0,
            "pass_to_pass_passed": 0,
        }

    @staticmethod
    def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        passed = sum(1 for r in results if r["status"] == "pass")
        failed = sum(1 for r in results if r["status"] == "fail")
        errors = sum(1 for r in results if r["status"] == "error")
        total_duration = sum(r["duration_seconds"] for r in results)
        return {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": passed / len(results) if results else 0,
            "total_duration_seconds": total_duration,
            "average_duration_seconds": total_duration / len(results) if results else 0,
        }


def _ensure_working_dir(agent: Any, repo_dir: Path) -> None:
    if hasattr(agent, "working_dir"):
        try:
            setattr(agent, "working_dir", str(repo_dir))
        except Exception:
            pass
    ensure = getattr(agent, "_ensure_working_dir", None)
    if callable(ensure):
        try:
            ensure()
        except Exception:
            pass


def _invoke_swebench_agent(agent: Any, instance: SWEBenchInstance, repo_dir: Path) -> None:
    task = instance.problem_statement
    if hasattr(agent, "run_agent_once"):
        agent.run_agent_once(
            system_prompt_name="swebench",
            messages=[{"role": "user", "content": task}],
            tools=[],
        )
    elif hasattr(agent, "run"):
        agent.run(task, working_dir=str(repo_dir))
    elif callable(agent):
        agent(task, working_dir=str(repo_dir))
    else:
        raise RuntimeError(
            f"agent for {instance.instance_id} exposes none of run/run_agent_once/__call__"
        )
