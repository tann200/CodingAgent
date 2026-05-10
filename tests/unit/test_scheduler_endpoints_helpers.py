import pytest
from fastapi import HTTPException

from src.server.scheduler_endpoints import (
    load_scheduler_worker,
    require_scheduler_admin_auth,
)


class _Req:
    pass


def test_require_scheduler_admin_auth_allows_http_exception_through():
    with pytest.raises(HTTPException) as exc:
        require_scheduler_admin_auth(
            _Req(),
            require_admin_auth=lambda request: (_ for _ in ()).throw(
                HTTPException(status_code=401, detail="Unauthorized")
            ),
            logger=type("L", (), {"warning": lambda *args, **kwargs: None})(),
        )
    assert exc.value.status_code == 401


def test_require_scheduler_admin_auth_maps_unexpected_errors_to_503():
    with pytest.raises(HTTPException) as exc:
        require_scheduler_admin_auth(
            _Req(),
            require_admin_auth=lambda request: (_ for _ in ()).throw(RuntimeError("boom")),
            logger=type("L", (), {"warning": lambda *args, **kwargs: None})(),
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == "Auth subsystem error"


def test_load_scheduler_worker_returns_module():
    worker = load_scheduler_worker(
        logger=type("L", (), {"warning": lambda *args, **kwargs: None})()
    )
    assert hasattr(worker, "list_jobs")
