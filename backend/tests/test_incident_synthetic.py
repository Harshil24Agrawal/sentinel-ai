"""The incident pipeline on synthetic traffic: fast, so it runs in the default suite."""

from helpers import Feed, ManagedRig


def test_thirty_percent_spike_opens_an_incident_within_fifteen_seconds() -> None:
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = ManagedRig(feed)
    rig.run(100)
    assert rig.manager.incidents() == []
    step = rig.t
    feed.p_error = 0.30
    rig.run(30)
    opened = [(t, e) for t, e in rig.events if e.kind == "opened"]
    assert len(opened) == 1
    tick, event = opened[0]
    assert 0 < tick - step <= 15
    assert event.incident.service == "svc"
    assert rig.manager.open_services() == {"svc"}


def test_incident_lifecycle_is_persisted_to_the_store() -> None:
    feed = Feed("svc", lines=25, p_error=0.02)
    rig = ManagedRig(feed, with_store=True)
    rig.run(100)
    feed.p_error = 0.50
    rig.run(40)
    feed.p_error = 0.02
    for _ in range(200):
        rig.tick()
        if not rig.manager.open_services():
            break
    assert rig.store is not None
    (stored,) = rig.store.list_incidents("resolved")
    assert stored.service == "svc" and stored.resolved_at is not None
    assert rig.store.list_incidents("open") == []
    kinds = [e["kind"] for e in rig.store.events(stored.id)]
    assert kinds[0] == "opened" and kinds[-1] == "resolved"
    assert rig.store.get_incident(stored.id) == rig.manager.get(stored.id)
