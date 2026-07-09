#!/usr/bin/env python3
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def load_records(paths: list[Path]) -> list[dict]:
    records = []
    for path in paths:
        with path.open(encoding='utf-8') as log_file:
            for line in log_file:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def summarize(records: list[dict]) -> str:
    if not records:
        return 'No request log records found.'

    by_event = Counter(record.get('event', 'unknown') for record in records)
    recs_latencies = [
        record['latency_ms']
        for record in records
        if record.get('event') == 'recs' and 'latency_ms' in record
    ]
    interact_actions = Counter()
    users_with_recs = set()
    users_with_interact = set()
    backfill_users = Counter()

    for record in records:
        if record.get('event') == 'recs':
            users_with_recs.add(record.get('user_id'))
            if record.get('backfill_count', 0) > 0:
                backfill_users[record.get('user_id')] += 1
        if record.get('event') == 'interact':
            users_with_interact.add(record.get('user_id'))
            for action in record.get('actions', []):
                interact_actions[action] += 1

    lines = [
        f'Total records: {len(records)}',
        f'Events: {dict(by_event)}',
        f'Unique users /recs: {len(users_with_recs)}',
        f'Unique users /interact: {len(users_with_interact)}',
        f'Interact actions: {dict(interact_actions)}',
    ]

    if recs_latencies:
        recs_latencies.sort()
        p95_index = max(0, int(len(recs_latencies) * 0.95) - 1)
        lines.extend([
            f'/recs latency ms: min={recs_latencies[0]:.1f} '
            f'p50={recs_latencies[len(recs_latencies)//2]:.1f} '
            f'p95={recs_latencies[p95_index]:.1f} '
            f'max={recs_latencies[-1]:.1f}',
        ])

    if backfill_users:
        top_backfill = backfill_users.most_common(10)
        lines.append(f'Users with backfill responses: {len(backfill_users)}')
        lines.append(f'Top backfill users: {top_backfill}')

    short_recs = [
        record for record in records
        if record.get('event') == 'recs' and record.get('returned_count', 0) < 10
    ]
    if short_recs:
        lines.append(f'Short /recs responses (<10): {len(short_recs)}')

    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='Summarize grader request logs')
    parser.add_argument(
        'paths',
        nargs='*',
        default=['data/grader_requests.jsonl', 'data/grader_requests_*.jsonl'],
        help='Log files or glob patterns',
    )
    args = parser.parse_args()

    paths: list[Path] = []
    for pattern in args.paths:
        matched = sorted(Path().glob(pattern))
        if matched:
            paths.extend(matched)
        else:
            path = Path(pattern)
            if path.exists():
                paths.append(path)

    records = load_records(paths)
    print(summarize(records))


if __name__ == '__main__':
    main()
