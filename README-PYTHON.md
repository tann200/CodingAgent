This project requires Python 3.11 for both development and CI.

Quick start:

- Create and activate the project venv using the project-local venv:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .[dev]
```

- Run unit tests:

```bash
python -m pytest tests/unit -q
```

Notes:
- `pyproject.toml` specifies `requires-python = ">=3.11,<3.12"` to ensure the project runs under 3.11.
- GitHub Actions workflow `/.github/workflows/ci.yml` is configured to use Python 3.11.

