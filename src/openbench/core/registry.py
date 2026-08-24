from __future__ import annotations

from openbench.core.capabilities import Instrument
from openbench.domain import Channel, Device


class DeviceRegistry:
    def __init__(self) -> None:
        self._instruments: dict[str, Instrument] = {}
        self._devices: dict[str, Device] = {}
        self._channels: dict[str, Channel] = {}

    def register(
        self,
        device: Device,
        instrument: Instrument,
        channels: tuple[Channel, ...] = (),
    ) -> None:
        if device.id != instrument.device_id:
            raise ValueError("Device and instrument IDs must match")
        if device.id in self._devices:
            raise ValueError(f"Device already registered: {device.id}")
        self._devices[device.id] = device
        self._instruments[device.id] = instrument
        for channel in channels:
            if channel.device_id != device.id:
                raise ValueError("Channel belongs to another device")
            if channel.id in self._channels:
                raise ValueError(f"Channel already registered: {channel.id}")
            self._channels[channel.id] = channel

    def devices(self) -> tuple[Device, ...]:
        return tuple(self._devices.values())

    def channels(self) -> tuple[Channel, ...]:
        return tuple(self._channels.values())

    def has_device(self, device_id: str) -> bool:
        return device_id in self._devices

    def device(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError as exc:
            raise KeyError(f"Unknown device: {device_id}") from exc

    def channel(self, channel_id: str) -> Channel:
        try:
            return self._channels[channel_id]
        except KeyError as exc:
            raise KeyError(f"Unknown channel: {channel_id}") from exc

    def update_channel(self, channel: Channel) -> None:
        current = self.channel(channel.id)
        if current.device_id != channel.device_id:
            raise ValueError("Updated channel belongs to another device")
        self._channels[channel.id] = channel

    def unregister(
        self,
        device_id: str,
    ) -> tuple[Device, Instrument, tuple[Channel, ...]]:
        device = self.device(device_id)
        instrument = self.instrument(device_id)
        channels = tuple(
            channel for channel in self._channels.values() if channel.device_id == device_id
        )
        for channel in channels:
            del self._channels[channel.id]
        del self._devices[device_id]
        del self._instruments[device_id]
        return device, instrument, channels

    def instrument(self, device_id: str) -> Instrument:
        try:
            return self._instruments[device_id]
        except KeyError as exc:
            raise KeyError(f"Unknown device: {device_id}") from exc
