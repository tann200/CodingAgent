import time
from src.core.logger import set_audit_log_path, audit_file_access


def test_audit_file_writer(tmp_path):
    logpath = tmp_path / 'audit.log'
    # ensure clean
    set_audit_log_path(logpath)
    # enqueue some audit events
    ok1 = audit_file_access(str(tmp_path / 'a.txt'), 'write', allowed=True)
    ok2 = audit_file_access(str(tmp_path / 'b.txt'), 'read', allowed=True)
    # wait up to 5 seconds for background worker to flush
    timeout = 5.0
    waited = 0.0
    interval = 0.1
    while waited < timeout:
        if logpath.exists():
            break
        time.sleep(interval)
        waited += interval
    # read log file
    assert logpath.exists(), 'audit log file not created'
    content = logpath.read_text(encoding='utf-8')
    assert 'FILE_WRITE' in content or 'FILE_ACCESS' in content
    # cleanup - stop worker
    set_audit_log_path(None)
