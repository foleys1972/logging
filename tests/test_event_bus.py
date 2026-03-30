import asyncio

import pytest

from wba_agent.events import Event, EventBus


@pytest.mark.asyncio
async def test_event_bus_dispatch():
    bus = EventBus()
    results = []

    async def handler(event: Event):
        results.append(event.payload["value"])

    bus.subscribe("test", handler)
    bus.start()

    await bus.publish(Event(type="test", payload={"value": 1}))
    await asyncio.sleep(0.05)

    assert results == [1]

    await bus.stop()


