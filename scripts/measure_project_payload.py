"""Measure read_github_project payload size on a few boards.

Run once against main and once against the branch, then paste both tables
into the PR for issue #141:

  python scripts/measure_project_payload.py --base-url http://localhost:8000 \
      --bearer-token "$TOKEN" nebari-dev:7 openteams-ai:1
"""

from __future__ import annotations

import argparse
import json

import httpx

CASES = [
    ("default", {}),
    ("max_items=500", {"max_items": 500}),
    ("query In Progress", {"query": 'status:"In Progress"'}),
]


def measure(base_url: str, bearer_token: str, boards: list[str]) -> None:
    headers = {"Authorization": f"Bearer {bearer_token}"}
    print("| board | case | bytes | ~tokens | items | total_count | authoritative |")
    print("|---|---|---:|---:|---:|---:|---|")
    with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=60) as client:
        for spec in boards:
            owner, number = spec.split(":")
            for label, extra in CASES:
                r = client.post(
                    f"/v1/connectors/github/projects/{number}/read",
                    json={"owner": owner, **extra},
                )
                if r.status_code != 200:
                    print(f"| {spec} | {label} | error {r.status_code} | | | | |")
                    continue
                body = r.json()
                size = len(json.dumps(body, separators=(",", ":")).encode())
                counts = body.get("counts") or {}
                print(
                    f"| {spec} | {label} | {size:,} | {size // 4:,} | {len(body['items'])} "
                    f"| {body['total_count']} | {counts.get('authoritative')} |"
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Collab Hub API base URL")
    parser.add_argument("--bearer-token", required=True)
    parser.add_argument("boards", nargs="+", help="owner:project_number, e.g. nebari-dev:7")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    measure(args.base_url, args.bearer_token, args.boards)