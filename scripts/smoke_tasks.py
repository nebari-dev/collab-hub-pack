"""Smoke assertions for the Collab Hub scheduled task REST contract."""

from __future__ import annotations

import argparse
import asyncio

import httpx

from smoke_connectors_google_drive import make_bearer_token


def task_payload(title: str = "Smoke task") -> dict:
    return {
        "title": title,
        "prompt": "Run the smoke task",
        "execution_device_id": "device-a",
        "agent_id": "agent-a",
        "schedule": {"kind": "manual"},
        "time_zone": "UTC",
    }


async def smoke_tasks(args: argparse.Namespace) -> str:
    token = args.bearer_token or make_bearer_token(args.user, args.org, args.workspace)
    headers = {"Authorization": f"Bearer {token}"}
    base_url = args.base_url.rstrip("/")
    api_base = args.api_base.rstrip("/")

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=20) as client:
        for attempt in range(30):
            try:
                health = await client.get("/health")
                health.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 29:
                    raise
                await asyncio.sleep(1)

        async with httpx.AsyncClient(base_url=base_url, timeout=20) as unauthenticated_client:
            unauthenticated = await unauthenticated_client.get(f"{api_base}/tasks/storage-policy")
            assert unauthenticated.status_code == 401

        policy = await client.get(f"{api_base}/tasks/storage-policy")
        policy.raise_for_status()
        assert policy.json()["max_retained_runs_per_task"] == 100

        created = await client.post(f"{api_base}/tasks", json=task_payload())
        created.raise_for_status()
        task = created.json()
        assert task["revision"] == 1

        heartbeat = await client.post(
            f"{api_base}/task-devices/heartbeat",
            json={"device_id": "device-a", "display_name": "Smoke laptop", "capabilities": ["docker"]},
        )
        heartbeat.raise_for_status()
        assert heartbeat.json()["device_id"] == "device-a"

        run_ids: list[str] = []
        for index in range(3):
            run = await client.post(
                f"{api_base}/tasks/{task['id']}/runs",
                json={
                    "id": f"run_smoke_{index}",
                    "created_at": f"2026-07-17T00:00:0{index}Z",
                    "task_revision": 1,
                    "schedule_revision": 1,
                    "occurrence_key": f"smoke-{index}",
                    "trigger": "manual",
                    "execution_device_id": "device-a",
                    "agent_id": "agent-a",
                    "task_snapshot": task,
                },
            )
            run.raise_for_status()
            run_ids.append(run.json()["id"])

        page = await client.get(f"{api_base}/task-runs?limit=1&offset=1")
        page.raise_for_status()
        assert [item["id"] for item in page.json()] == ["run_smoke_1"]

        updated = await client.patch(
            f"{api_base}/tasks/runs/{run_ids[-1]}",
            json={"status": "failed", "reason": "smoke", "result_metadata": {"error": "smoke error"}},
        )
        updated.raise_for_status()
        assert updated.json()["result_metadata"] == {"error": "smoke error"}

        notifications = await client.get(f"{api_base}/task-notifications")
        notifications.raise_for_status()
        assert notifications.json()[0]["kind"] == "run_failed"

        deleted = await client.delete(f"{api_base}/tasks/{task['id']}?delete_history=true")
        deleted.raise_for_status()
        remaining = await client.get(f"{api_base}/task-runs")
        remaining.raise_for_status()
        assert remaining.json() == []

    return task["id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-base", default="/v1")
    parser.add_argument("--bearer-token", default=None)
    parser.add_argument("--user", default="task-smoke-user")
    parser.add_argument("--org", default="task-smoke-org")
    parser.add_argument("--workspace", default="task-smoke-workspace")
    return parser.parse_args()


def main() -> None:
    task_id = asyncio.run(smoke_tasks(parse_args()))
    print(task_id)


if __name__ == "__main__":
    main()
