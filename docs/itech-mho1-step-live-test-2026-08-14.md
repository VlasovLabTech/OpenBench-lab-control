# ITECH IT6054C + MHO1 sink-step live test — 2026-08-14

## Bench and invariant scope state

- ITECH IT6054C-800-225, public example serial `ITECH-DEMO-0001`, temporary
  Windows `COMn` port at 115200 baud.
- Micsig MHO14-200, serial `MHO1-DEMO-0001`, firmware `2.154.75`.
- External source: approximately 12 V with a 10 A current limit.
- Scope remained in operator-configured `YT`, CH1-CH4 displayed, 2,750-point
  depth, and 250 kSa/s. The experiment sent no physical scope-settings update.
- Sink steps were 0, 2, 4, 7, 9, and 11 A. ITECH Output remained enabled between
  steps and was forced OFF after every series.

Each point saved a direct screenshot, six configured scalar Measurements, a
combined numeric waveform CSV, and the four original CH1-CH4 ASCII payloads.
Every waveform contained 2,750 points.

## Standalone timing

| Operation | Result |
| --- | ---: |
| Apply six MHO1 Measurement slots, one-time setup | 1.030-1.032 s |
| Read six MHO1 Measurements | 73.77-198.86 ms; 104.22 ms mean |
| MHO1 Screen + Measurements | 0.3076-0.7600 s |
| MHO1 Screen + Measurements + CH1-CH4 Data | 0.8800-1.0451 s |
| ITECH compact V/I/P, immediately repeated | 9.23-11.20 ms after a 1.290 s cold read |
| ITECH compact V/I/P, spaced hardware reads | 0.833-1.242 s excluding one cache hit |

## Three complete series

All three series used a 4.0 s point-start interval. All 18 frames returned
`status=ok`, four channels, PNG/JPEG Screen, Measurements CSV, waveform CSV, and
four raw ASCII channel files.

| Metric | N | Min | Mean | Median | P95 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ITECH live current command | 15 | 0.0670 s | 0.0872 s | 0.0894 s | 0.0995 s | 0.0995 s |
| ITECH V/I/P hardware read | 18 | 1.0949 s | 1.1797 s | 1.1421 s | 1.3161 s | 1.3161 s |
| MHO1 complete frame API call | 18 | 0.9522 s | 1.0309 s | 1.0092 s | 1.1664 s | 1.1664 s |
| Complete point | 18 | 1.6141 s | 1.7528 s | 1.7329 s | 1.8768 s | 1.8768 s |
| Actual point-start interval | 15 | 4.0001 s | 4.0004 s | 4.0005 s | 4.0008 s | 4.0008 s |
| Series preparation | 3 | 9.804 s | 9.9697 s | 10.014 s | 10.091 s | 10.091 s |
| ITECH Output ON response | 3 | 5.769 s | 9.3913 s | 5.831 s | 16.574 s | 16.574 s |
| Common CSV recording start | 3 | 1.934 s | 1.941 s | 1.943 s | 1.946 s | 1.946 s |

The 11 A command intentionally exceeded the external source's 10 A limit. The
measured sink current was 10.022-10.024 A and the input collapsed to 51.5-53.2
mV, as expected.

## Artifacts

- `.openbench/data/captures/sessions/20260814_1812_rec_itech_mho1.csv`
- `.openbench/data/captures/sessions/20260814_1812_rec_itech_mho1/`
- `.openbench/data/captures/sessions/20260814_1815_rec_itech_mho1.csv`
- `.openbench/data/captures/sessions/20260814_1815_rec_itech_mho1/`
- `.openbench/data/captures/sessions/20260814_1816_rec_itech_mho1.csv`
- `.openbench/data/captures/sessions/20260814_1816_rec_itech_mho1/`

Final verification: ITECH Output OFF with no faults; MHO1 still in YT at 2,750
points and 250 kSa/s with CH1-CH4 displayed.
