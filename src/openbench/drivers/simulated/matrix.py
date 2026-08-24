from __future__ import annotations

from collections.abc import Sequence

from openbench.domain import MatrixConnection, MatrixPort

DEFAULT_PORTS: tuple[MatrixPort, ...] = (
    MatrixPort(id="dut.vout", name="DUT output voltage", type="signal"),
    MatrixPort(id="dut.gnd", name="DUT ground", type="return"),
    MatrixPort(id="meter.voltage_hi", name="Meter voltage HI", type="measurement_input"),
    MatrixPort(id="meter.voltage_lo", name="Meter voltage LO", type="measurement_input"),
    MatrixPort(id="scope.ch1", name="Oscilloscope CH1", type="scope_input"),
    MatrixPort(id="scope.gnd", name="Oscilloscope ground", type="return"),
    MatrixPort(id="generator.out1", name="Generator output 1", type="source"),
    MatrixPort(id="source.main", name="Main DC source", type="source"),
    MatrixPort(id="source.aux", name="Auxiliary DC source", type="source"),
)


class SimulatedMatrix:
    device_id = "sim_matrix_main"

    def __init__(self) -> None:
        self._active_connections: tuple[MatrixConnection, ...] = ()

    async def identify(self) -> str:
        return "OpenBench Simulated Relay Matrix"

    def list_ports(self) -> Sequence[MatrixPort]:
        return DEFAULT_PORTS

    def apply_connections(self, connections: Sequence[MatrixConnection]) -> None:
        self._active_connections = tuple(connections)

    def open_all(self) -> None:
        self._active_connections = ()

    @property
    def active_connections(self) -> tuple[MatrixConnection, ...]:
        return self._active_connections
