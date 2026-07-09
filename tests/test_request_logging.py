import json
from pathlib import Path

from request_logging import archive_request_log, log_request


def test_request_log_write_and_archive(tmp_path, monkeypatch):
    log_path = tmp_path / 'grader_requests.jsonl'
    monkeypatch.setattr('request_logging.REQUEST_LOG_PATH', log_path)
    monkeypatch.setattr('request_logging.DATA_DIR', tmp_path)

    log_request('recs', 'recs', user_id='u1', returned_count=10)
    assert log_path.exists()

    archive_path = archive_request_log()
    assert archive_path is not None
    assert archive_path.name.startswith('grader_requests_')
    assert not log_path.exists()

    with archive_path.open(encoding='utf-8') as log_file:
        record = json.loads(log_file.readline())
    assert record['event'] == 'recs'
    assert record['user_id'] == 'u1'
