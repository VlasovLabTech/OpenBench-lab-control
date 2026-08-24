# MHO1 scalar measurement timing live test — 2026-08-02

Hardware: Micsig MHO14-200, serial `MHO1-DEMO-0001`, firmware `2.154.75`, LAN
`192.0.2.10`. The oscilloscope remained in STOP and the generator settings
were not changed.

The configured ten-item profile was split evenly between CH1 and CH2:
amplitude, peak-to-peak, RMS, frequency, and one voltage extremum per channel.
Configuration uses `CLEAR`, paced `OPEN` commands, and a final stabilization
delay; it completed in about 3.3 seconds. Repeated reads used the separate
`POST /api/v1/oscilloscopes/{device_id}/measurements/read` path with no profile
changes and zero artificial delay between SCPI query/response transactions.

Ten consecutive ten-value reads succeeded 10/10:

- instrument time: 87.8 ms minimum, 116.3 ms mean, 170.6 ms maximum;
- full HTTP time: 90.6 ms minimum, 124.2 ms mean, 173.6 ms maximum;
- all 100 returned values had status `ok`.

A separate 20-item configuration test returned the first ten values and marked
all following ten unavailable. The working profile was then restored to five
CH1 plus five CH2 values. This confirms a ten-slot global firmware limit, so the
Dashboard and API enforce ten across CH1-CH4.

The snapshot path originally resent `STOP` even though acquisition was already
stopped; this made the direct screenshot fall back to scope-file storage and
took about 5.6 seconds. Skipping that redundant command kept the direct transfer
active. The final screenshot plus ten-measurement snapshot completed in
335.9 ms and did not add a file under `/pictures/Screenshots`.

A full four-channel card-cycle timing then ran sequentially through the same
supported API: four scope-local BIN saves/downloads followed by the direct
screenshot plus ten scalar reads. All four BIN files were complete at 11,256
bytes. BIN acquisition took 834.6 ms, the snapshot stage took 304.0 ms, and the
whole cycle took 1,138.6 ms. The two-second Dashboard minimum therefore leaves
about 860 ms of headroom. A deliberately heavier two-channel comparison that
also included the screenshot and all ten card measurements took 766.1 ms.
