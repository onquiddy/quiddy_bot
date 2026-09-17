import pytest
from quiddy.core.events import EventBus, Event


@pytest.mark.asyncio
async def test_event_bus_isolates_handlers():
    bus = EventBus(handler_timeout=1)
    seen = []

    async def bad(event):
        raise RuntimeError("boom")

    async def good(event):
        seen.append(event)

    bus.subscribe(Event, bad)
    bus.subscribe(Event, good)
    event = Event()
    await bus.publish(event)
    assert seen == [event]
