"""The run API's first slice (``/v1/runs``, behind ``cog_runs``): launch, list, cancel.

The API records intent on a SQLite Track and reads status from it. A run
controller over the same Track, here with an in-memory executor, is what
advances the runs: the two never call each other.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest
import pytest_asyncio
from collab_hub_execution import InMemoryCogExecutor, LifecycleRunner, ResultEnvelope, SqliteTrackStore
from collab_hub_execution.controller import RunController
from httpx import ASGITransport, AsyncClient

from collab_hub_api.config import Config
from collab_hub_api.core import make_app

MANIFEST = '[workspace]\nname = "{name}"\n\n[tasks]\nserve = "python serve.py"\n'


def package(root, name):
    (root / name).mkdir(parents=True)
    (root / name / "pixi.toml").write_text(MANIFEST.format(name=name))
    (root / name / "pixi.lock").write_text("version: 6\n")


@pytest.fixture
def runs_config(tmp_path) -> Config:
    for name in ("echo", "slow", "hidden"):
        package(tmp_path / "cogs", name)
    # A directory that is allowlisted and has a manifest, and still cannot run: it has no lock.
    package(tmp_path / "cogs", "unlocked")
    (tmp_path / "cogs" / "unlocked" / "pixi.lock").unlink()
    return Config.parse({
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"active_state": {"backend": "memory"}, "history": {"backend": "memory"},
                   "usage": {"backend": "memory"}, "mcp_session_manager_enabled": False},
        "tasks": {"backend": "memory"},
        "features": {"cog_runs": True},
        "runs": {"track_path": str(tmp_path / "track.sqlite"), "packages": [str(tmp_path / "cogs")],
                 "allow": ["echo", "slow", "unlocked"]},
    })


@pytest_asyncio.fixture
async def runs_client(runs_config, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    monkeypatch.setenv("DEV_AUTH_WORKSPACE", "default")
    app = make_app(runs_config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


@pytest.fixture
def controller(runs_config):
    """A controller over the API's Track; ``release`` lets the slow Cog answer."""
    release = threading.Event()

    def slow(entry_point, value, **feedback):
        release.wait(10)
        return ResultEnvelope.success({"slept": True})

    executor = InMemoryCogExecutor({"echo": lambda entry_point, value, **feedback: {"echo": value}, "slow": slow})
    runner = LifecycleRunner(executor=executor, track=SqliteTrackStore(runs_config.runs.track_path))
    controller = RunController(runner, poll_interval=0.01)
    controller.release = release
    yield controller
    release.set()


def settle(controller, until=lambda: True):
    """Let the controller act on what the Track holds, until ``until`` and nothing is in flight."""
    for _ in range(500):
        controller.tick()
        if until() and controller.idle():
            return
        threading.Event().wait(0.01)
    raise AssertionError("the controller did not settle")


ECHO = {"steps": [{"name": "greet", "cog": "echo", "entry_point": "run", "input": {"text": "hi"}}]}
SLOW = {"steps": [{"name": "wait", "cog": "slow", "entry_point": "run"}, {"name": "after", "cog": "echo",
                                                                           "entry_point": "run"}]}


async def test_a_submitted_run_waits_for_a_controller_and_completes_once_one_picks_it_up(runs_client, controller):
    created = await runs_client.post("/v1/runs", json=ECHO)
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "SUBMITTED" and run["ended"] is False
    assert run["backend"] == "none" and run["location"] == "local"  # so a client assumes nothing of either
    assert run["submitted_by"] == "dev-user" and run["submitted_by_name"] is None  # dev auth carries no name
    assert run["steps"] == [{"name": "greet", "cog": "echo", "entry_point": "run", "state": "pending",
                             "attempt": None, "error": None, "output": None, "output_ref": None}]
    # The API started nothing: the run advances only once a controller reads the Track.
    assert (await runs_client.get(f"/v1/runs/{run['id']}")).json()["status"] == "SUBMITTED"
    settle(controller)
    done = (await runs_client.get(f"/v1/runs/{run['id']}")).json()
    assert done["status"] == "COMPLETED" and done["ended"] is True
    assert done["steps"][0]["state"] == "completed" and done["steps"][0]["attempt"] == 0
    # The single run carries what the Cog answered with; the listing does not.
    assert done["steps"][0]["output"] == {"echo": {"text": "hi"}}
    [listed] = (await runs_client.get("/v1/runs")).json()["items"]
    assert listed["status"] == "COMPLETED" and listed["steps"][0]["output"] is None


async def test_a_run_can_be_named_and_is_listed_by_that_name(runs_client):
    created = (await runs_client.post("/v1/runs", json={**ECHO, "name": "echo on Claude"})).json()
    assert created["name"] == "echo on Claude"
    assert (await runs_client.get(f"/v1/runs/{created['id']}")).json()["name"] == "echo on Claude"
    unnamed = (await runs_client.post("/v1/runs", json=ECHO)).json()
    assert unnamed["name"] is None
    assert [item["name"] for item in (await runs_client.get("/v1/runs")).json()["items"]] == [None, "echo on Claude"]
    for bad in ("", " leading space", "x" * 65, "semi;colon"):
        assert (await runs_client.post("/v1/runs", json={**ECHO, "name": bad})).status_code == 422, bad


async def test_runs_are_listed_newest_first_filtered_by_status_and_paged(runs_client, controller):
    first = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    settle(controller)
    second = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    listing = (await runs_client.get("/v1/runs")).json()
    assert [item["id"] for item in listing["items"]] == [second, first] and listing["next_offset"] is None
    completed = (await runs_client.get("/v1/runs", params={"status": "completed"})).json()
    assert [item["id"] for item in completed["items"]] == [first]
    page = (await runs_client.get("/v1/runs", params={"limit": 1})).json()
    assert [item["id"] for item in page["items"]] == [second] and page["next_offset"] == 1
    rest = (await runs_client.get("/v1/runs", params={"limit": 1, "offset": 1})).json()
    assert [item["id"] for item in rest["items"]] == [first] and rest["next_offset"] is None


async def test_cancelling_a_run_mid_step_ends_it_cancelled_with_its_actor(runs_client, controller, runs_config):
    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    track = SqliteTrackStore(runs_config.runs.track_path)
    for _ in range(500):  # until the slow step is in flight
        controller.tick()
        if any(event.event_type == "interaction_started" for event in track.replay(run_id)):
            break
        threading.Event().wait(0.01)
    assert (await runs_client.get(f"/v1/runs/{run_id}")).json()["steps"][0]["state"] == "running"
    accepted = await runs_client.post(f"/v1/runs/{run_id}/cancel")
    assert accepted.status_code == 202
    assert accepted.json()["cancel_requested_by"] == "dev-user" and accepted.json()["status"] == "RUNNING"
    again = await runs_client.post(f"/v1/runs/{run_id}/cancel")  # asking twice records it once
    assert again.status_code == 202
    assert [e.event_type for e in track.replay(run_id)].count("cancel_requested") == 1
    controller.tick()  # the controller delivers the request, then the step in flight returns
    controller.release.set()
    settle(controller)
    ended = (await runs_client.get(f"/v1/runs/{run_id}")).json()
    assert ended["status"] == "CANCELLED"
    assert [step["state"] for step in ended["steps"]] == ["cancelled", "pending"]  # the second never started
    [cancelled] = [event for event in track.replay(run_id) if event.event_type == "cancelled"]
    assert cancelled.payload == {"actor": "dev-user"}
    conflict = await runs_client.post(f"/v1/runs/{run_id}/cancel")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "run_ended"
    assert "CANCELLED" in conflict.json()["error"]["message"]


async def test_a_run_cancelled_before_any_controller_picked_it_up_never_starts(runs_client, controller):
    run_id = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    assert (await runs_client.post(f"/v1/runs/{run_id}/cancel")).status_code == 202
    settle(controller)
    ended = (await runs_client.get(f"/v1/runs/{run_id}")).json()
    assert ended["status"] == "CANCELLED" and ended["steps"][0]["state"] == "pending"
    assert controller.runner.executor.materialized == []


@pytest.mark.parametrize(("cog", "why"), [("hidden", "not allowlisted"), ("absent", "not allowlisted"),
                                          ("unlocked", "has no pixi.lock"),
                                          ("../echo", "not a package name")])
async def test_a_step_naming_a_cog_the_hub_cannot_launch_is_refused_naming_those_it_can(runs_client, cog, why):
    body = {"steps": [{"name": "s", "cog": cog, "entry_point": "run"}]}
    refused = await runs_client.post("/v1/runs", json=body)
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "cog_not_launchable" and "echo, slow" in error["message"]
    assert error["details"]["launchable"] == ["echo", "slow"] and why in error["details"]["refused"][cog]
    assert (await runs_client.get("/v1/runs")).json()["items"] == []  # nothing was recorded


@pytest.mark.parametrize("body", [
    {"steps": []},
    {"steps": [{"name": "s", "cog": "echo"}]},
    {"steps": [{"name": "s", "cog": "echo", "entry_point": "run", "gate": {"escalate": "sometimes"}}]},
    {"steps": [{"name": "s", "cog": "echo", "entry_point": "run"}, {"name": "s", "cog": "echo", "entry_point": "run"}]},
    {"steps": [{"name": "s", "cog": "echo", "entry_point": "run"}], "run_id": "mine"},
])
async def test_a_malformed_op_is_refused(runs_client, body):
    assert (await runs_client.post("/v1/runs", json=body)).status_code == 422


async def test_a_steps_gate_is_recorded_with_the_op(runs_client, controller):
    body = {"steps": [{"name": "s", "cog": "echo", "entry_point": "run", "gate": {"escalate": "always"}}]}
    run_id = (await runs_client.post("/v1/runs", json=body)).json()["id"]
    settle(controller)
    waiting = (await runs_client.get(f"/v1/runs/{run_id}")).json()
    assert waiting["status"] == "WAITING_AT_GATE" and waiting["steps"][0]["state"] == "waiting_at_gate"


async def test_another_organization_can_neither_see_nor_cancel_a_run(runs_client, monkeypatch):
    run_id = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    monkeypatch.setenv("DEV_AUTH_ORG", "another-org")
    assert (await runs_client.get("/v1/runs")).json()["items"] == []
    missing = await runs_client.get(f"/v1/runs/{run_id}")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "run_not_found"
    assert (await runs_client.post(f"/v1/runs/{run_id}/cancel")).status_code == 404
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    assert (await runs_client.get(f"/v1/runs/{run_id}")).json()["cancel_requested_by"] is None


async def test_unknown_and_malformed_run_ids(runs_client):
    assert (await runs_client.get("/v1/runs/run-000000000000")).status_code == 404
    assert (await runs_client.post("/v1/runs/run-000000000000/cancel")).status_code == 404
    assert (await runs_client.get("/v1/runs/has spaces")).status_code == 422


async def test_the_run_api_is_refused_without_a_caller(runs_config, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(runs_config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/runs")).status_code == 401
            assert (await client.post("/v1/runs", json=ECHO)).status_code == 401


async def test_without_the_flag_there_is_no_run_api(dev_client):
    assert (await dev_client.get("/v1/runs")).status_code == 404
    assert "/v1/runs" not in (await dev_client.get("/openapi.json")).json()["paths"]


def test_the_api_constructs_no_executor_and_reaches_execution_only_from_the_run_router():
    # ADR-0002 invariant 3: the API writes intent and reads the Track. Nothing in it imports the
    # runner, the controller or an executor, and the execution package is imported in one module,
    # which is itself imported only when the flag is on.
    source = Path(__file__).resolve().parents[1] / "src" / "collab_hub_api"
    forbidden = ("runner", "controller", "kubernetes", "locations.local", "locations.launcher", "backends")
    importers = []
    for path in sorted(source.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            used = [module for module in modules if module.split(".")[0] == "collab_hub_execution"]
            if used:
                importers.append(path.name)
            for module in used:
                assert not any(f".{part}" in f".{module.split('.', 1)[-1]}" or module.endswith("Executor")
                               or module.endswith("LifecycleRunner") for part in forbidden), (path.name, module)
    assert set(importers) == {"runs.py"}


def test_the_flag_without_a_track_stops_startup(tmp_path):
    config = Config.parse({"storage": {"frames_path": str(tmp_path)}, "features": {"cog_runs": True}})
    with pytest.raises(RuntimeError, match="runs.track_path"):
        make_app(config)


async def test_with_no_package_directory_nothing_is_launchable(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    config = Config.parse({
        "storage": {"frames_path": str(tmp_path / "frames")},
        "frames": {"active_state": {"backend": "memory"}, "history": {"backend": "memory"},
                   "usage": {"backend": "memory"}, "mcp_session_manager_enabled": False},
        "tasks": {"backend": "memory"}, "features": {"cog_runs": True},
        "runs": {"track_path": str(tmp_path / "track.sqlite")}})
    app = make_app(config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post("/v1/runs", json=ECHO)
    assert refused.status_code == 422 and "are: none" in refused.json()["error"]["message"]


async def test_a_package_that_does_not_exist_is_refused_without_naming_the_hubs_directories(
        runs_config, tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_USER", "dev-user")
    monkeypatch.setenv("DEV_AUTH_ORG", "dev-org")
    runs_config.runs.allow = None
    app = make_app(runs_config)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post("/v1/runs", json={"steps": [{"name": "s", "cog": "absent",
                                                                     "entry_point": "run"}]})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["refused"] == {"absent": "no package 'absent'"}
    assert str(tmp_path) not in refused.text


def test_package_directories_arrive_from_the_environment_as_one_path_list(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLAB_HUB_API__RUNS__PACKAGES", f"{tmp_path / 'a'}:{tmp_path / 'b'}")
    monkeypatch.setenv("COLLAB_HUB_API__RUNS__TRACK_PATH", str(tmp_path / "t.sqlite"))
    runs = Config.parse({"storage": {"frames_path": str(tmp_path)}}).runs
    assert runs.packages == [str(tmp_path / "a"), str(tmp_path / "b")] and runs.track_path.endswith("t.sqlite")


async def test_the_hub_names_the_cogs_it_can_launch(runs_client):
    assert (await runs_client.get("/v1/runs/launchable")).json() == {"items": ["echo", "slow"]}


async def test_a_turn_is_recorded_answered_through_the_track_and_read_back(runs_client, runs_config):
    from collab_hub_execution import intents

    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    asked = await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "sum 1 2"})
    assert asked.status_code == 202
    turn = asked.json()
    assert turn["state"] == "pending" and turn["text"] == "sum 1 2" and turn["asked_by"] == "dev-user"
    # The controller's half, as it records an answer.
    track = SqliteTrackStore(runs_config.runs.track_path)
    intents.answer_turn(track, run_id, turn["turn"], text="1 + 2 = 3")
    answered = (await runs_client.get(f"/v1/runs/{run_id}/turns/{turn['turn']}")).json()
    assert answered["state"] == "answered" and answered["answer"] == "1 + 2 = 3"
    missing = await runs_client.get(f"/v1/runs/{run_id}/turns/000000000000")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "turn_not_found"
    assert (await runs_client.get(f"/v1/runs/{run_id}/turns/not-a-turn")).status_code == 422
    assert (await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": ""})).status_code == 422


async def test_a_run_that_ended_takes_no_turns(runs_client, controller):
    run_id = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    settle(controller)
    refused = await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "hello"})
    assert refused.status_code == 409 and "has ended COMPLETED" in refused.json()["error"]["message"]


async def test_another_organization_can_neither_talk_to_a_run_nor_read_its_turns(runs_client, monkeypatch):
    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    turn = (await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "hi"})).json()["turn"]
    monkeypatch.setenv("DEV_AUTH_ORG", "another-org")
    assert (await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": "hi"})).status_code == 404
    assert (await runs_client.get(f"/v1/runs/{run_id}/turns/{turn}")).status_code == 404


async def test_a_run_shows_who_submitted_it_by_name_and_is_scoped_by_principal(runs_config, monkeypatch):
    from collab_hub_api.frames.auth import AuthContext, DisplayIdentity, get_auth_context

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(runs_config)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user="3b8ec34f", home_org_id="dev-org", workspace_id="default",
        display=DisplayIdentity(name="Dev User", email="dev@example.com"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            run = (await client.post("/v1/runs", json=ECHO)).json()
            listed = (await client.get("/v1/runs")).json()["items"]
    assert run["submitted_by"] == "3b8ec34f" and run["submitted_by_name"] == "Dev User"
    assert [item["submitted_by_name"] for item in listed] == ["Dev User"]


async def test_refusals_on_the_run_api_use_the_error_envelope(runs_client):
    # The API's error envelope, as on the other /v1 routes, with what was refused in its details.
    bad_name = await runs_client.post("/v1/runs", json={**ECHO, "name": "a/b"})
    assert bad_name.status_code == 422
    error = bad_name.json()["error"]
    assert error["code"] == "validation_error" and error["details"][0]["loc"] == ["body", "name"]
    bad_id = await runs_client.get("/v1/runs/has spaces")
    assert bad_id.status_code == 422 and bad_id.json()["error"]["code"]


async def test_a_turn_over_the_limit_in_bytes_is_refused_with_422_not_500(runs_client):
    run_id = (await runs_client.post("/v1/runs", json=SLOW)).json()["id"]
    from collab_hub_execution import intents

    text = "字" * (intents.MAX_TURN_TEXT // 3 + 1)  # fewer characters than the limit, more bytes
    assert len(text) < intents.MAX_TURN_TEXT < len(text.encode())
    refused = await runs_client.post(f"/v1/runs/{run_id}/turns", json={"text": text})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "turn_too_long"


async def test_a_run_that_cannot_be_read_is_left_out_of_the_list_and_not_found_alone(runs_client, runs_config):
    from collab_hub_execution.track import SCHEMA_VERSION, TrackEvent

    good = (await runs_client.post("/v1/runs", json=ECHO)).json()["id"]
    track = SqliteTrackStore(runs_config.runs.track_path)
    by = {"user": "dev-user", "org_id": "dev-org", "workspace_id": "default"}
    track.append(TrackEvent(run_id="run-broken00000", event_type="op_submitted",
                            payload={"op": {"run_id": "run-broken00000", "steps": []}, "submitted_by": by},
                            schema=SCHEMA_VERSION))
    track.append(TrackEvent(run_id="run-broken00000", event_type="gate_decided", payload={"outcome": "approve"},
                            schema=SCHEMA_VERSION))
    assert [item["id"] for item in (await runs_client.get("/v1/runs")).json()["items"]] == [good]
    assert (await runs_client.get("/v1/runs/run-broken00000")).status_code == 404
