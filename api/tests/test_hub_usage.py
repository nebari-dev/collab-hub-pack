"""Usage across the whole hub, for an operator.

The existing usage endpoints answer for one workspace and are readable by any
member of it. This is the other question -- what is happening on this
deployment -- and it is the operator's alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from collab_hub_api.frames.usage import (
    InMemoryUsageStore,
    UnavailableUsageStore,
    UsageUnavailableError,
)


def seeded() -> InMemoryUsageStore:
    store = InMemoryUsageStore()
    store.record_user_seen("org-a", "default", "u-1", "alice@example.com")
    store.record_user_seen("org-a", "default", "u-2", "bob@example.com")
    store.record_user_seen("org-b", "default", "u-3", "carol@example.com")
    store.record_event("org-a", "default", "u-1", "chat")
    store.record_event("org-a", "default", "u-1", "chat")
    store.record_event("org-b", "default", "u-3", "frame.write")
    return store


def test_the_hub_summary_counts_people_and_events_across_organizations():
    summary = seeded().hub_summary()

    assert summary.users_total == 3
    assert summary.events_total == 3
    assert dict(summary.events) == {"chat": 2, "frame.write": 1}
    assert {org.org_id: (org.users, org.events) for org in summary.organizations} == {
        "org-a": (2, 2),
        "org-b": (1, 1),
    }


def test_one_person_in_two_organizations_is_counted_once_for_the_hub():
    """A hub total that double-counted would overstate the deployment's size."""

    store = seeded()
    store.record_user_seen("org-b", "default", "u-1", "alice@example.com")

    summary = store.hub_summary()

    assert summary.users_total == 3
    assert {org.org_id: org.users for org in summary.organizations} == {"org-a": 2, "org-b": 2}


def test_the_window_bounds_events_and_leaves_the_roster_alone():
    """Events are what happened in a window; people are current state."""

    store = seeded()
    future = datetime.now(timezone.utc) + timedelta(days=1)

    summary = store.hub_summary(since=future)

    assert summary.events_total == 0
    assert summary.users_total == 3


def test_a_deployment_without_a_database_refuses_rather_than_answering_zero():
    with pytest.raises(UsageUnavailableError):
        UnavailableUsageStore().hub_summary()


def test_the_postgres_summary_asks_the_database_to_count_rather_than_streaming_rows():
    """A hub with a large roster must not be counted in this process."""

    from collab_hub_api.frames.usage import PostgresUsageStore

    store = PostgresUsageStore.__new__(PostgresUsageStore)
    statements: list[str] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            statements.append(" ".join(str(sql).split()))
            self._sql = " ".join(str(sql).split())
            return self

        def fetchall(self):
            return []

        def fetchone(self):
            return {"users": 7}

    store._connect = lambda: Conn()  # type: ignore[attr-defined]

    summary = store.hub_summary()

    assert summary.users_total == 7
    assert any("count(DISTINCT user_id)" in sql for sql in statements)
    assert not any("SELECT user_id, email" in sql for sql in statements)


def test_the_window_is_half_open_like_the_database_query(monkeypatch):
    """``since`` is inclusive and ``until`` exclusive, as Postgres runs it, so
    adjacent windows never count one event twice and this store cannot hide a
    boundary bug the production one would show."""

    from collab_hub_api.frames import usage

    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(usage, "_now", lambda: instant)
    store = InMemoryUsageStore()
    store.record_event("org-a", "default", "u-1", "chat")

    assert store.hub_summary(since=instant).events_total == 1
    assert store.hub_summary(until=instant).events_total == 0
