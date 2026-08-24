from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from openbench.api.schemas import MeasurementOut
from openbench.bootstrap import ApplicationContext

router = APIRouter()


@router.websocket("/ws/measurements")
async def measurement_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    context: ApplicationContext = websocket.app.state.context
    try:
        async with context.event_bus.subscribe() as queue:
            while True:
                measurement = await queue.get()
                await websocket.send_json(
                    MeasurementOut.from_domain(measurement).model_dump(mode="json")
                )
    except WebSocketDisconnect:
        return
