# MHO1 fast binary waveform live test — 2026-08-02

## Bench state

- Micsig MHO14-200, serial `MHO1-DEMO-0001`, firmware `2.154.75`
- STOP, 5,500-point memory, NORMAL waveform mode, WORD display format
- CH1 and CH2 carried the same approximately 20 MHz sine signal
- All operations used the documented OpenBench JSON API

## ASCII confirmation

`DATA:ASCii?` captured CH1 and CH2 as 1,100 numeric voltage samples each. The
local CSV contains 1,100 data rows and spans -2.46082 V to +2.43611 V on both
channels. No screenshot or pixel-derived data participated.

## Undocumented fast BIN behavior

The official Micsig manual documents `FORMat WORD` followed by `DATA?`; it does
not document `DATA:BIN?`. A bounded diagnostic of the latter produced:

1. 4,400 bytes / 1,100 signed 32-bit codes immediately after a two-channel
   ASCII acquisition.
2. An empty block after an OpenBench restart with the same visible scope state.
3. A second empty block on the same fresh waveform session.
4. 4,400 bytes / 1,100 signed 32-bit codes immediately after a one-channel
   ASCII acquisition.

The successful raw artifact is
`mho1_fast_binary_ch1_20260801T213500500698Z.bin`. Its first eight little-endian
signed codes are `603, 622, 641, 659, 676, 693, 709, 725`.

Against the immediately preceding ASCII capture, a zero-intercept fit gives
`0.00247069938562223 V/code`; maximum absolute residual across all 1,100 points
is 9.24 microvolts. The fast binary block therefore contains the same waveform
samples, not screen pixels.

## Priming lifetime test

After a fresh OpenBench connection, one CH1 ASCII acquisition returned 5,500
points in 15.731 seconds and saved
`session_prime_ascii_20260801T214608460961Z.csv` (228,875 bytes). Without any
additional ASCII request, the following BIN probes produced:

| Probe | Change before query | Result | Wall time |
| --- | --- | ---: | ---: |
| 1 | CH1, baseline | 22,000 bytes / 5,500 int32 points | 10.161 s |
| 2 | source CH2 | empty | 5.107 s |
| 3 | source CH1 | empty | 0.128 s |
| 4 | memory depth 11,000 | empty | 5.080 s |
| 5 | CH2, timebase 2 ms/div | empty | 5.137 s |

The successful file is
`mho1_fast_binary_ch1_20260801T214618643979Z.bin`.

## Scope-storage test

The initial direct and configured workflows failed in RUN and STOP. A video of
the scope notification exposed the cause: the failed name was displayed as
`"ob220947ch1".bin`, while a manual success used `2608020001.bin`. Although the
manual specifies a quoted ASCII filename, firmware `2.154.75` preserved the
quotes literally and rejected the name.

OpenBench was changed to omit `STORage:SAVE:FILename`, leaving the scope's own
numeric name untouched. In STOP, `SAVE CH1` followed by `SAVE:STARt` then
created `/files/binwave/2608020002.bin`, detected it, downloaded it locally, and
returned success in 2.047 seconds. The file contains a 256-byte header followed
by 5,500 signed 16-bit samples: exactly one channel, 11,256 bytes total.

The final path sends a safe numeric filename without quotes, so its exact
`/files/binwave/<name>.bin` URL is known and no directory listing is needed.
The scope exposes a 256-byte header before it has finished writing; OpenBench
therefore reads the point count from that header and retries the same URL until
all `256 + points * 2` bytes are available.

Delay sweeps in 5 ms steps established the hardware boundary. Five milliseconds
between storage commands and five milliseconds before `SAVE:STARt` worked;
zero milliseconds in either position failed. The 5/5 ms setting downloaded one
complete channel in 151.6 ms and sequential CH1+CH2 in 395 ms. Each resulting
file was 11,256 bytes with 5,500 samples; the scope remained in STOP.

A final ten-run CH1+CH2 series at the fixed 5/5 ms production delays completed
10/10. Every run returned two full 11,256-byte files and all 20 scope paths were
unique. Total per-run times were 307.9 ms minimum, 353.4 ms mean, and 461.2 ms
maximum.

## Current conclusion

One ASCII request is not enough for a session: it primes exactly the next BIN
query. Fast BIN is therefore slower overall than using ASCII directly and
remains diagnostic. Production CSV acquisition continues to use the stable
fast ASCII path.
