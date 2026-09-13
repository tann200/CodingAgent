"""Audit PHASE-3 item 3.3: SWE-bench integration harness."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from src.core.evaluation import regression
from src.core.evaluation import scenario_evaluator as se
from src.core.evaluation import cli
from src.core.evaluation.swebench import (
    SWEBenchInstance,
    SWEBenchRunner,
    checkout_repo,
    grade_runnable,
    load_instances,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True
    )


CALC_BROKEN = "def add(a, b):\n    return None\n"
TEST_ADD = (
    "def test_add():\n"
    "    from calc import add\n"
    "    assert add(2, 3) == 5\n"
)
CALC_FIXED = "def add(a, b):\n    return a + b\n"


def make_git_repo(root: Path, files: dict, commit_msg: str = "base") -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "tests")
    _git(root, "config", "user.email", "tests@example.com")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", commit_msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def make_instance(
    repo: Path,
    base_commit: str,
    *,
    instance_id: str = "fixture-1",
    fail_to_pass=None,
    pass_to_pass=None,
    test_command=None,
    test_patch=None,
) -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id=instance_id,
        repo=str(repo),
        base_commit=base_commit,
        problem_statement="Fix calc.add to return a + b so the test passes.",
        fail_to_pass=fail_to_pass or ["test_calc.py::test_add"],
        pass_to_pass=pass_to_pass or [],
        test_command=test_command,
        test_patch=test_patch,
    )


@pytest.fixture
def buggy_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    commit = make_git_repo(repo, {"calc.py": CALC_BROKEN, "test_calc.py": TEST_ADD})
    return repo, commit


def _reg_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# data model + loading
# ---------------------------------------------------------------------------


class TestInstanceModel:
    def test_from_dict(self) -> None:
        inst = SWEBenchInstance.from_dict(
            {
                "instance_id": "a",
                "repo": "org/repo",
                "base_commit": "abc123",
                "problem_statement": "p",
                "fail_to_pass": ["t::x"],
                "PASS_TO_PASS": ["t::y"],
            }
        )
        assert inst.fail_to_pass == ["t::x"]
        assert inst.pass_to_pass == ["t::y"]
        assert inst.instance_id == "a"

    def test_from_dict_missing_field_raises(self) -> None:
        with pytest.raises(ValueError):
            SWEBenchInstance.from_dict({"instance_id": "a", "repo": "r", "base_commit": "c"})

    def test_to_dict_roundtrip(self) -> None:
        inst = make_instance(Path("x"), "abc")
        assert SWEBenchInstance.from_dict(inst.to_dict()) == inst


class TestLoadInstances:
    def test_json_list(self, tmp_path: Path) -> None:
        p = tmp_path / "instances.json"
        p.write_text(json.dumps([make_instance(Path("r"), "c").to_dict()]))
        assert len(load_instances(p)) == 1

    def test_json_dict_keyed(self, tmp_path: Path) -> None:
        p = tmp_path / "instances.json"
        inst = make_instance(Path("r"), "c")
        p.write_text(json.dumps({inst.instance_id: inst.to_dict()}))
        loaded = load_instances(p)
        assert loaded[0].instance_id == "fixture-1"

    def test_jsonl(self, tmp_path: Path) -> None:
        p = tmp_path / "instances.jsonl"
        p.write_text(
            json.dumps(make_instance(Path("r"), "c").to_dict())
            + "\n"
            + json.dumps(make_instance(Path("r"), "c", instance_id="two").to_dict())
            + "\n"
        )
        assert [i.instance_id for i in load_instances(p)] == ["fixture-1", "two"]

    def test_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "set"
        d.mkdir()
        (d / "a.json").write_text(json.dumps([make_instance(Path("r"), "c").to_dict()]))
        (d / "b.jsonl").write_text(json.dumps(make_instance(Path("r"), "c", instance_id="two").to_dict()) + "\n")
        assert len(load_instances(d)) == 2

    def test_missing_source_raises(self) -> None:
        with pytest.raises(ValueError):
            load_instances("definitely_not_here.json")

    def test_corrupt_jsonl_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.jsonl"
        p.write_text("{not json\n")
        with pytest.raises(json.JSONDecodeError):
            load_instances(p)


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


class TestGrading:
    def test_checkout_at_base_commit(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        out = checkout_repo(make_instance(repo, commit), tmp_path / "dest")
        head = _git(out, "rev-parse", "HEAD").stdout.strip()
        assert head == commit
        assert (out / "calc.py").read_text() == CALC_BROKEN

    def test_empty_patch_is_fail(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(repo, commit)
        repo_dir = checkout_repo(inst, tmp_path / "dest")
        grade = grade_runnable(repo_dir, inst)
        assert grade.status == "fail"
        assert "no changes" in (grade.error or "")

    def test_correct_fix_passes(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(repo, commit)
        repo_dir = checkout_repo(inst, tmp_path / "dest")
        (repo_dir / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
        grade = grade_runnable(repo_dir, inst)
        assert grade.status == "pass", grade.output
        assert grade.fail_to_pass_passed == 1
        assert grade.patch and "calc.py" in grade.patch

    def test_wrong_fix_fails(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(repo, commit)
        repo_dir = checkout_repo(inst, tmp_path / "dest")
        (repo_dir / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        grade = grade_runnable(repo_dir, inst)
        assert grade.status == "fail"
        assert grade.patch is not None

    def test_patch_includes_new_file(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(repo, commit)
        repo_dir = checkout_repo(inst, tmp_path / "dest")
        (repo_dir / "new_file.txt").write_text("hello", encoding="utf-8")
        grade = grade_runnable(repo_dir, inst)
        assert grade.patch and "new_file.txt" in grade.patch

    def test_test_command_override(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(
            repo,
            commit,
            test_command="python -c 'from calc import add; assert add(2, 3) == 5'",
            fail_to_pass=[],
        )
        repo_dir = checkout_repo(inst, tmp_path / "dest")
        (repo_dir / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
        assert grade_runnable(repo_dir, inst).status == "pass"

    def test_test_patch_apply_error(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(
            repo,
            commit,
            test_patch="diff --not a valid patch\n+++ b/x\n@@ -0\n",
            fail_to_pass=[],
            test_command="python -c 'pass'",
        )
        repo_dir = checkout_repo(inst, tmp_path / "dest")
        (repo_dir / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
        grade = grade_runnable(repo_dir, inst)
        assert grade.status == "error"
        assert "test_patch" in (grade.error or "")


# ---------------------------------------------------------------------------
# runner + CLI
# ---------------------------------------------------------------------------


class TestRunner:
    def test_end_to_end_pass(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(repo, commit)

        def factory():
            class _Agent:
                def run(self, task: str, working_dir: str | None = None) -> None:
                    assert working_dir is not None
                    (Path(working_dir) / "calc.py").write_text(
                        CALC_FIXED, encoding="utf-8"
                    )

            return _Agent()

        result = SWEBenchRunner(workdir=str(tmp_path / "runs")).run_instance(inst, factory)
        assert result["status"] == "pass"
        assert result["patch"] and "calc.py" in result["patch"]
        summary = SWEBenchRunner.summarize([result])
        assert summary["passed"] == 1
        assert summary["total"] == 1

    def test_repo_checkout_error_is_error(self, tmp_path: Path) -> None:
        inst = make_instance(tmp_path / "no_such_repo", "abc123")
        result = SWEBenchRunner(workdir=str(tmp_path / "runs")).run_instance(
            inst, lambda: object()
        )
        assert result["status"] == "error"
        assert "repo checkout failed" in (result["error"] or "")

    def test_summarize_counts(self, buggy_repo, tmp_path: Path) -> None:
        repo, commit = buggy_repo
        inst = make_instance(repo, commit, instance_id="broken")
        runner = SWEBenchRunner(workdir=str(tmp_path / "runs"))

        def factory():
            class _Noop:
                def run(self, task: str, working_dir: str | None = None) -> None:
                    return None

            return _Noop()

        result = runner.run_instance(inst, factory)
        assert result["status"] == "fail"
        summary = SWEBenchRunner.summarize([result])
        assert summary["passed"] == 0
        assert summary["failed"] == 1


class TestCliSwebench:
    @pytest.fixture
    def source(self, buggy_repo, tmp_path: Path) -> Path:
        repo, commit = buggy_repo
        p = tmp_path / "instances.jsonl"
        p.write_text(
            json.dumps(make_instance(repo, commit).to_dict()) + "\n", encoding="utf-8"
        )
        return p

    def _run(self, source: Path, factory_name: str, tmp_path: Path):
        return cli.main(
            [
                "swebench",
                "--source",
                str(source),
                "--agent",
                factory_name,
                "--workdir",
                str(tmp_path / "runs"),
            ]
        )

    def test_run_passes(self, source: Path, tmp_path: Path, capsys) -> None:
        mod = _reg_module("_swe_agent_pass")
        setattr(
            mod,
            "make_agent",
            lambda: type(
                "A",
                (),
                {
                    "run": lambda self, task, working_dir=None: (
                        Path(working_dir) / "calc.py"
                    ).write_text(CALC_FIXED, encoding="utf-8")
                },
            )(),
        )
        rc = self._run(source, "_swe_agent_pass:make_agent", tmp_path)
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "fixture-1" in out
        assert "1/1 passed" in out

    def test_run_noop_fails(self, source: Path, tmp_path: Path, capsys) -> None:
        mod = _reg_module("_swe_agent_noop")
        setattr(
            mod,
            "make_agent",
            lambda: type(
                "A", (), {"run": lambda self, task, working_dir=None: None}
            )(),
        )
        rc = self._run(source, "_swe_agent_noop:make_agent", tmp_path)
        assert rc == 0
        assert "0/1 passed" in capsys.readouterr().out

    def test_regression_exit_code(self, source: Path, tmp_path: Path, capsys) -> None:
        base = tmp_path / "base.json"
        regression.save_baseline(
            base,
            [
                se.ScenarioResult(
                    scenario_name="fixture-1",
                    status="pass",
                    start_time=__import__("datetime").datetime.now(),
                    end_time=__import__("datetime").datetime.now(),
                    duration_seconds=0.1,
                )
            ],
        )
        mod = _reg_module("_swe_agent_reg")
        setattr(
            mod,
            "make_agent",
            lambda: type(
                "A", (), {"run": lambda self, task, working_dir=None: None}
            )(),
        )
        rc = cli.main(
            [
                "swebench",
                "--source",
                str(source),
                "--agent",
                "_swe_agent_reg:make_agent",
                "--workdir",
                str(tmp_path / "runs"),
                "--baseline",
                str(base),
            ]
        )
        assert rc == 1
        assert "REGRESSION DETECTED" in capsys.readouterr().out

    def test_report_output(self, source: Path, tmp_path: Path, capsys) -> None:
        mod = _reg_module("_swe_agent_report")
        setattr(
            mod,
            "make_agent",
            lambda: type(
                "A",
                (),
                {
                    "run": lambda self, task, working_dir=None: (
                        Path(working_dir) / "calc.py"
                    ).write_text(CALC_FIXED, encoding="utf-8")
                },
            )(),
        )
        out = tmp_path / "report.json"
        rc = cli.main(
            [
                "swebench",
                "--source",
                str(source),
                "--agent",
                "_swe_agent_report:make_agent",
                "--workdir",
                str(tmp_path / "runs"),
                "--output",
                str(out),
            ]
        )
        assert rc == 0
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["kind"] == "swebench"
        assert doc["summary"]["passed"] == 1


def test_package_exports_swebench() -> None:
    import src.core.evaluation as ev

    for name in ("SWEBenchInstance", "SWEBenchRunner", "load_instances"):
        assert hasattr(ev, name), name
        assert name in ev.__all__