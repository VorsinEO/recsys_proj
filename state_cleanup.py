import subprocess
from datetime import datetime
from pathlib import Path

from config import DATA_DIR, INTERACTIONS_PATH, RABBITMQ_QUEUE_NAME


def archive_interactions_csv(
    interactions_path: Path = INTERACTIONS_PATH,
    data_dir: Path = DATA_DIR,
) -> Path | None:
    if not interactions_path.exists():
        return None

    if interactions_path.stat().st_size == 0:
        interactions_path.unlink()
        return None

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_path = data_dir / f'interactions_{timestamp}.csv'
    data_dir.mkdir(parents=True, exist_ok=True)
    interactions_path.rename(archive_path)
    return archive_path


def purge_rabbitmq_queue(queue_name: str = RABBITMQ_QUEUE_NAME) -> bool:
    try:
        result = subprocess.run(
            ['rabbitmqctl', 'purge_queue', queue_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
