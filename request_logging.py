import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA_DIR

REQUEST_LOG_PATH = DATA_DIR / 'grader_requests.jsonl'


def log_request(service: str, event: str, **fields: Any) -> None:
    record = {
        'ts': time.time(),
        'service': service,
        'event': event,
        **fields,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with REQUEST_LOG_PATH.open('a', encoding='utf-8') as log_file:
        log_file.write(json.dumps(record, ensure_ascii=False) + '\n')


def archive_request_log() -> Path | None:
    if not REQUEST_LOG_PATH.exists():
        return None
    if REQUEST_LOG_PATH.stat().st_size == 0:
        REQUEST_LOG_PATH.unlink()
        return None

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_path = DATA_DIR / f'grader_requests_{timestamp}.jsonl'
    REQUEST_LOG_PATH.rename(archive_path)
    return archive_path
