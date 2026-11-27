"""Built-in 23-scenario evaluation suite for CodingAgent.

Extracted from ``src/core/evaluation/scenario_evaluator.py`` (TASK-43).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from src.core.evaluation.scenario_evaluator import Scenario


def get_default_scenarios() -> List[Scenario]:
    """Get a set of default evaluation scenarios.

    P4-T1: Extended from 3 trivial scenarios to 23 covering:
    single-file edits, multi-file refactoring, bug fix, task disambiguation,
    tool failure recovery, plan adherence, context boundary, and delegation.
    """
    from src.core.evaluation.scenario_evaluator import Scenario

    return [
        # ── Original 3 ──────────────────────────────────────────────────────
        Scenario(
            name="simple_function",
            description="Create a simple function that returns a string",
            task="Create a file called hello.py with a function hello() that returns 'Hello World'",
            expected_files={"hello.py": "def hello"},
            test_command="python -c \"from hello import hello; assert hello() == 'Hello World'\"",
            difficulty="easy",
            category="single_file_edit",
        ),
        Scenario(
            name="class_definition",
            description="Create a class with methods",
            task="Create a file calculator.py with a Calculator class that has add and subtract methods",
            expected_files={"calculator.py": "class Calculator:"},
            test_command='python -c "from calculator import Calculator; c = Calculator(); assert c.add(1, 2) == 3"',
            difficulty="easy",
            category="single_file_edit",
        ),
        Scenario(
            name="test_creation",
            description="Create a simple unit test",
            task="Create a test file test_math.py with a pytest test function for a math operation (e.g. addition).",
            expected_files={"test_math.py": "def test_"},
            test_command="python -m pytest -q test_math.py",
            difficulty="easy",
            category="single_file_edit",
        ),

        # ── Single-file edits ────────────────────────────────────────────────
        Scenario(
            name="rename_function",
            description="Rename a function in an existing file",
            task="Rename the function `compute_total` to `calculate_total` in utils.py",
            setup_files={
                "utils.py": (
                    "def compute_total(items):\n"
                    "    return sum(items)\n"
                ),
            },
            expected_files={"utils.py": "def calculate_total"},
            test_command=(
                "python -c \""
                "import utils; "
                "assert not hasattr(utils, 'compute_total') and utils.calculate_total([1, 2, 3]) == 6\""
            ),
            difficulty="easy",
            category="single_file_edit",
            tags=["rename"],
        ),
        Scenario(
            name="add_docstring",
            description="Add a docstring to an existing function",
            task="Add a Google-style docstring to the function `parse_config` in config.py that describes what it does and its parameters.",
            setup_files={
                "config.py": (
                    "def parse_config(path):\n"
                    "    with open(path) as f:\n"
                    "        return f.read()\n"
                ),
            },
            expected_files={"config.py": '"""'},
            test_command=(
                "python -c \""
                "import ast; "
                "tree = ast.parse(open('config.py').read()); "
                "fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'parse_config'); "
                "assert ast.get_docstring(fn)\""
            ),
            difficulty="easy",
            category="single_file_edit",
            tags=["docstring"],
        ),
        Scenario(
            name="fix_off_by_one",
            description="Fix an off-by-one error in a loop",
            task=(
                "Fix the off-by-one error in the `get_last_n` function in list_utils.py. "
                "The function should return the last n items of a list but currently misses the last element."
            ),
            setup_files={
                "list_utils.py": (
                    "def get_last_n(items, n):\n"
                    "    return items[len(items)-n:len(items)-1]\n"  # bug: -1 drops last
                ),
            },
            expected_files={"list_utils.py": "def get_last_n"},
            test_command=(
                "python -c \""
                "from list_utils import get_last_n; "
                "assert get_last_n([1,2,3,4,5], 3) == [3,4,5], 'off-by-one not fixed'\""
            ),
            difficulty="medium",
            category="single_file_edit",
            tags=["bug_fix", "off_by_one"],
        ),
        Scenario(
            name="add_type_hints",
            description="Add type hints to an untyped function",
            task="Add Python type hints to all parameters and the return type of the `merge_dicts` function in merge.py.",
            setup_files={
                "merge.py": (
                    "def merge_dicts(a, b):\n"
                    "    result = {}\n"
                    "    result.update(a)\n"
                    "    result.update(b)\n"
                    "    return result\n"
                ),
            },
            expected_files={"merge.py": "->"},
            test_command=(
                "python -c \""
                "import inspect, merge; "
                "sig = inspect.signature(merge.merge_dicts); "
                "assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values()); "
                "assert sig.return_annotation is not inspect.Parameter.empty; "
                "assert merge.merge_dicts({'a': 1}, {'b': 2}) == {'a': 1, 'b': 2}\""
            ),
            difficulty="easy",
            category="single_file_edit",
            tags=["type_hints"],
        ),
        Scenario(
            name="add_error_handling",
            description="Add try/except error handling to a function",
            task=(
                "Add proper error handling to `read_json_file` in file_reader.py. "
                "It should raise a ValueError with a descriptive message if the file "
                "does not exist or contains invalid JSON."
            ),
            setup_files={
                "file_reader.py": (
                    "import json\n\n"
                    "def read_json_file(path):\n"
                    "    with open(path) as f:\n"
                    "        return json.load(f)\n"
                ),
            },
            expected_files={"file_reader.py": "ValueError"},
            test_command=(
                "python -c \""
                "from file_reader import read_json_file; "
                "open('bad.json', 'w').write('{bad'); "
                "open('good.json', 'w').write('{\\\"ok\\\": true}'); "
                "assert read_json_file('good.json') == {'ok': True}; "
                "errs = 0\n"
                "try:\n read_json_file('missing.json')\nexcept ValueError:\n errs += 1\n"
                "try:\n read_json_file('bad.json')\nexcept ValueError:\n errs += 1\n"
                "assert errs == 2\""
            ),
            difficulty="medium",
            category="single_file_edit",
            tags=["error_handling"],
        ),

        # ── Multi-file refactoring ───────────────────────────────────────────
        Scenario(
            name="move_class_update_imports",
            description="Move a class to a new module and update all import references",
            task=(
                "Move the `UserValidator` class from validators.py into a new file "
                "user_validator.py. Update the import in app.py so it imports "
                "`UserValidator` from `user_validator` instead of `validators`."
            ),
            setup_files={
                "validators.py": (
                    "class UserValidator:\n"
                    "    def validate(self, user):\n"
                    "        return bool(user.get('name'))\n"
                ),
                "app.py": (
                    "from validators import UserValidator\n\n"
                    "def process(user):\n"
                    "    v = UserValidator()\n"
                    "    return v.validate(user)\n"
                ),
            },
            expected_files={
                "user_validator.py": "class UserValidator",
                "app.py": "from user_validator import UserValidator",
            },
            test_command=(
                "python -c \""
                "from user_validator import UserValidator; "
                "from app import process; "
                "assert process({'name': 'A'}) is True and process({}) is False\""
            ),
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "move_class"],
        ),
        Scenario(
            name="split_module",
            description="Split a monolithic module into two focused modules",
            task=(
                "Split monolith.py into two files: "
                "math_utils.py (containing `add` and `multiply`) and "
                "string_utils.py (containing `upper_case` and `reverse_string`). "
                "Keep the functions identical."
            ),
            setup_files={
                "monolith.py": (
                    "def add(a, b): return a + b\n"
                    "def multiply(a, b): return a * b\n"
                    "def upper_case(s): return s.upper()\n"
                    "def reverse_string(s): return s[::-1]\n"
                ),
            },
            expected_files={
                "math_utils.py": "def add",
                "string_utils.py": "def upper_case",
            },
            test_command=(
                "python -c \""
                "from math_utils import add, multiply; "
                "from string_utils import upper_case, reverse_string; "
                "assert add(2, 3) == 5 and multiply(2, 4) == 8 and upper_case('hi') == 'HI' and reverse_string('abc') == 'cba'\""
            ),
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "split"],
        ),
        Scenario(
            name="add_shared_constants",
            description="Extract magic numbers into a shared constants module",
            task=(
                "Extract the magic number `3.14159` from geometry.py and "
                "the magic number `9.81` from physics.py into a new file "
                "constants.py. Update both files to import from constants.py."
            ),
            setup_files={
                "geometry.py": "def circle_area(r): return 3.14159 * r * r\n",
                "physics.py": "def weight(mass): return mass * 9.81\n",
            },
            expected_files={
                "constants.py": "3.14159",
                "geometry.py": "constants",
                "physics.py": "constants",
            },
            test_command=(
                "python -c \""
                "import constants, geometry, physics; "
                "assert '3.14159' not in open('geometry.py').read() and '9.81' not in open('physics.py').read(); "
                "assert abs(geometry.circle_area(1) - 3.14159) < 1e-4 and abs(physics.weight(2) - 19.62) < 1e-4\""
            ),
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "constants"],
        ),

        # ── Bug introduction / fix ───────────────────────────────────────────
        Scenario(
            name="fix_null_pointer",
            description="Fix a None-reference bug",
            task=(
                "Fix the bug in profile.py: `get_display_name` crashes with "
                "AttributeError when `user` is None. It should return 'Anonymous' instead."
            ),
            setup_files={
                "profile.py": (
                    "def get_display_name(user):\n"
                    "    return user['name'].title()\n"
                ),
            },
            expected_files={"profile.py": "Anonymous"},
            test_command=(
                "python -c \""
                "from profile import get_display_name; "
                "assert get_display_name(None) == 'Anonymous'\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["null_check"],
        ),
        Scenario(
            name="fix_resource_leak",
            description="Fix a file-handle resource leak",
            task=(
                "Fix the resource leak in logger.py: `append_log` opens the file "
                "but never closes it. Use a context manager to ensure the file "
                "is always closed."
            ),
            setup_files={
                "logger.py": (
                    "def append_log(path, message):\n"
                    "    f = open(path, 'a')\n"
                    "    f.write(message + '\\n')\n"
                ),
            },
            expected_files={"logger.py": "with open"},
            test_command=(
                "python -c \""
                "src = open('logger.py').read(); assert 'with ' in src; "
                "import logger; logger.append_log('out.log', 'hello'); "
                "assert 'hello' in open('out.log').read()\""
            ),
            difficulty="easy",
            category="bug_fix",
            tags=["resource_leak"],
        ),
        Scenario(
            name="fix_mutable_default_arg",
            description="Fix the mutable default argument anti-pattern",
            task=(
                "Fix the mutable default argument bug in accumulator.py: "
                "`collect` uses `items=[]` as a default argument, which causes "
                "values to persist across calls. Fix it using the `None` sentinel pattern."
            ),
            setup_files={
                "accumulator.py": (
                    "def collect(value, items=[]):\n"
                    "    items.append(value)\n"
                    "    return items\n"
                ),
            },
            expected_files={"accumulator.py": "None"},
            test_command=(
                "python -c \""
                "from accumulator import collect; "
                "a = collect(1); b = collect(2); c = collect(3, [0]); "
                "assert a == [1] and b == [2] and a is not b and c == [0, 3], 'mutable default not fixed'\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["mutable_default"],
        ),

        # ── Context boundary (requires reading multiple files) ───────────────
        Scenario(
            name="trace_call_chain",
            description="Follow a 5-file call chain to find the root cause",
            task=(
                "The function `run_pipeline` in pipeline.py raises a KeyError when a record "
                "is missing optional fields. Trace through the call chain "
                "(pipeline.py → stage.py → transform.py → validator.py → schema.py) and fix "
                "validator.py using safe lookup (`record.get(f)`) so missing fields do not "
                "raise a KeyError and partial records pass through safely."
            ),
            setup_files={
                "schema.py": "REQUIRED_FIELDS = ['name', 'value']\n",
                "validator.py": (
                    "from schema import REQUIRED_FIELDS\n\n"
                    "def validate(record):\n"
                    "    for f in REQUIRED_FIELDS:\n"
                    "        _ = record[f]  # KeyError if missing\n"
                    "    return True\n"
                ),
                "transform.py": (
                    "from validator import validate\n\n"
                    "def transform(record):\n"
                    "    validate(record)\n"
                    "    return {k: str(v) for k, v in record.items()}\n"
                ),
                "stage.py": (
                    "from transform import transform\n\n"
                    "def process_stage(records):\n"
                    "    return [transform(r) for r in records]\n"
                ),
                "pipeline.py": (
                    "from stage import process_stage\n\n"
                    "def run_pipeline(data):\n"
                    "    return process_stage(data)\n"
                ),
            },
            expected_files={"validator.py": "get("},
            test_command=(
                "python -c \""
                "from pipeline import run_pipeline; "
                "assert run_pipeline([{'name': 'a'}, {'name': 'b', 'value': 2}]) == [{'name': 'a'}, {'name': 'b', 'value': '2'}]\""
            ),
            difficulty="hard",
            category="context_boundary",
            tags=["multi_file_read", "call_chain"],
        ),
        Scenario(
            name="dependency_graph_refactor",
            description="Refactor code that spans 5 interdependent files",
            task=(
                "Add a `timeout` parameter (default=30) to `fetch_data` in fetcher.py. "
                "Propagate it through: fetcher.py → session.py → connection.py → "
                "retry.py → config.py (where the default should be defined as "
                "`DEFAULT_TIMEOUT = 30`)."
            ),
            setup_files={
                "config.py": "# Connection configuration\nMAX_RETRIES = 3\n",
                "retry.py": "def with_retries(fn, retries):\n    return fn()\n",
                "connection.py": "def connect(host): return {'host': host}\n",
                "session.py": "from connection import connect\ndef open_session(host): return connect(host)\n",
                "fetcher.py": "from session import open_session\ndef fetch_data(url): return open_session(url)\n",
            },
            expected_files={"config.py": "DEFAULT_TIMEOUT"},
            test_command=(
                "python -c \""
                "import inspect, config, fetcher; "
                "assert config.DEFAULT_TIMEOUT == 30 and inspect.signature(fetcher.fetch_data).parameters['timeout'].default == 30\""
            ),
            difficulty="hard",
            category="context_boundary",
            tags=["parameter_propagation"],
        ),

        # ── Plan adherence ───────────────────────────────────────────────────
        Scenario(
            name="multi_step_crud",
            description="Implement a multi-step CRUD module in order",
            task=(
                "Create a simple in-memory CRUD store in store.py. "
                "Follow this exact order: "
                "1. Define a `Store` class with an empty `_data` dict. "
                "2. Add a `create(key, value)` method. "
                "3. Add a `read(key)` method that returns None if missing. "
                "4. Add an `update(key, value)` method. "
                "5. Add a `delete(key)` method. "
                "6. Write a test file test_store.py that tests all four operations."
            ),
            expected_files={
                "store.py": "class Store",
                "test_store.py": "def test_",
            },
            test_command=(
                "python -c \""
                "from store import Store; s = Store(); "
                "s.create('k', 1); assert s.read('k') == 1; "
                "s.update('k', 2); assert s.read('k') == 2; "
                "s.delete('k'); assert s.read('k') is None\""
            ),
            difficulty="medium",
            category="plan_adherence",
            tags=["crud", "multi_step"],
        ),

        # ── Tool failure recovery ────────────────────────────────────────────
        Scenario(
            name="recover_from_syntax_error",
            description="Agent self-corrects after writing syntactically invalid Python",
            task=(
                "Create a file sorter.py with a function `sort_descending(items)` "
                "that returns the list sorted in descending order. "
                "Make sure the file is valid Python."
            ),
            expected_files={"sorter.py": "def sort_descending"},
            test_command=(
                "python -c \""
                "import ast; ast.parse(open('sorter.py').read()); "
                "from sorter import sort_descending; "
                "assert sort_descending([3,1,2]) == [3,2,1]\""
            ),
            difficulty="easy",
            category="tool_failure_recovery",
            tags=["syntax_check", "self_correction"],
        ),
        Scenario(
            name="recover_from_import_error",
            description="Agent fixes a broken import after creating it",
            task=(
                "Create main.py that imports and uses `greet` from greeter.py. "
                "Also create greeter.py with the `greet(name)` function that "
                "returns f'Hello, {name}!'. Ensure both files work together."
            ),
            expected_files={
                "greeter.py": "def greet",
                "main.py": "from greeter import greet",
            },
            test_command="python -c \"import main\"",
            difficulty="easy",
            category="tool_failure_recovery",
            tags=["import_fix"],
        ),

        # ── Task disambiguation ──────────────────────────────────────────────
        Scenario(
            name="ambiguous_format_request",
            description="Interpret an ambiguous formatting request sensibly",
            task=(
                "Format the data in records.py. "
                "The file contains a list of dicts; make sure they are consistently "
                "formatted with one dict per line and keys in alphabetical order."
            ),
            setup_files={
                "records.py": (
                    "records = [\n"
                    "    {'z': 3, 'a': 1, 'name': 'Alice'},\n"
                    "    {'name': 'Bob', 'a': 2, 'z': 4},\n"
                    "]\n"
                ),
            },
            expected_files={},
            test_command=(
                "python -c \""
                "import records; "
                "dict_lines = [l for l in open('records.py').read().splitlines() if '{' in l and '}' in l]; "
                "assert len(dict_lines) == 2 and len(records.records) == 2 "
                "and all(list(r.keys()) == sorted(r.keys()) for r in records.records)\""
            ),
            difficulty="medium",
            category="disambiguation",
            tags=["formatting"],
        ),

        # ── Delegation / subagent ────────────────────────────────────────────
        Scenario(
            name="delegate_test_generation",
            description="Delegate test writing to a subagent",
            task=(
                "We have a module math_ops.py. Delegate to a subagent the task of "
                "writing comprehensive pytest tests for it in test_math_ops.py. "
                "The tests should cover `add`, `subtract`, `multiply`, and `divide` "
                "(divide should raise ZeroDivisionError for divisor=0)."
            ),
            setup_files={
                "math_ops.py": (
                    "def add(a, b): return a + b\n"
                    "def subtract(a, b): return a - b\n"
                    "def multiply(a, b): return a * b\n"
                    "def divide(a, b):\n"
                    "    if b == 0:\n"
                    "        raise ZeroDivisionError('Cannot divide by zero')\n"
                    "    return a / b\n"
                ),
            },
            expected_files={"test_math_ops.py": "def test_"},
            test_command="python -m pytest -q test_math_ops.py",
            difficulty="medium",
            category="delegation",
            tags=["subagent", "test_generation"],
        ),
        Scenario(
            name="delegate_documentation",
            description="Delegate README generation to a subagent",
            task=(
                "Delegate to a subagent the task of writing a README.md for this project. "
                "The README should include: project title, description, installation, "
                "and a usage example. Base it on the code in app.py."
            ),
            setup_files={
                "app.py": (
                    "\"\"\"Simple web scraper utility.\"\"\"\n\n"
                    "def scrape(url, selector):\n"
                    "    \"\"\"Scrape *selector* elements from *url*.\"\"\"\n"
                    "    pass\n"
                ),
            },
            expected_files={"README.md": "# "},
            test_command=(
                "python -c \""
                "txt = open('README.md').read().lower(); "
                "assert '#' in txt and 'install' in txt and ('usage' in txt or 'example' in txt)\""
            ),
            difficulty="easy",
            category="delegation",
            tags=["subagent", "documentation"],
        ),
        Scenario(
            name="circular_import_fix",
            description="Resolve a circular import between two modules",
            task=(
                "Fix the circular import between mod_a.py and mod_b.py while "
                "keeping both `get_a()` and `get_b()` working."
            ),
            setup_files={
                "mod_a.py": "from mod_b import VALUE_B\nVALUE_A = 1\ndef get_a(): return VALUE_A + VALUE_B\n",
                "mod_b.py": "from mod_a import VALUE_A\nVALUE_B = 2\ndef get_b(): return VALUE_A + VALUE_B\n",
            },
            expected_files={"mod_a.py": "def get_a", "mod_b.py": "def get_b"},
            test_command=(
                "python -c \""
                "from mod_a import get_a; from mod_b import get_b; "
                "assert get_a() == 3 and get_b() == 3\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["circular_import"],
        ),
    ]
