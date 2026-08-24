# MHO1 minimal fast-ASCII live test - 2026-08-02

## Purpose and constraints

This test isolated the smallest literal waveform transaction requested for the
Micsig MHO14-200. It did not use the OpenBench Codex skill, the OpenBench API,
the existing MHO1 driver, scalar measurements, screenshots, state queries,
read-backs, retries, or setting changes.

The oscilloscope was assumed to be in RUN. Its acquisition state, waveform
source, mode, format, memory depth, and preamble were deliberately not queried.

## Bench

- Instrument known from the preceding bench record: Micsig MHO14-200, serial
  `MHO1-DEMO-0001`, firmware `2.154.75`.
- LAN target: `192.0.2.10:5025`.
- Before the test, the OpenBench server was stopped with
  `scripts/stop-openbench.ps1`.
- The host then had no listener on TCP port 8000 and no active TCP connection to
  `192.0.2.10`.
- Probe implementation: `scripts/mho1_ascii_minimal_probe.py`, Python standard
  library only, with no imports from `openbench`.

## Exact transaction

The probe made three fresh TCP connections and sent exactly these SCPI lines:

1. `:MENU:STOP` at `2026-08-02T11:17:41.898293Z`.
2. `:WAVeform:DATA:ASCii?` at `2026-08-02T11:17:42.003450Z`, after a fixed
   100 ms host-side delay.
3. `:MENU:RUN` at `2026-08-02T11:17:42.022687Z` from a `finally` block.

There was one acquisition attempt and zero automatic retries.

## Result

The ASCII query returned a syntactically recognizable definite-block header,
but its declared point count was zero. No waveform values or CSV rows were
available. The complete transaction took 155.646 ms.

The probe rejected the zero count as a failed capture. The RUN command was then
sent successfully; there was no RUN transport error. No SCPI status query was
used to verify the final state.

The local ignored session record is:

`.openbench/data/captures/mho1-ascii-minimal/20260802T111741_867076Z/transaction.json`

The first probe revision rejected the zero count before persisting the header,
so this run has metadata but no raw artifact. The probe and its tests were then
updated to preserve future zero-point headers before reporting the failure.

## Delay-only retest

A separately approved second one-shot kept the identical three-command contract
and changed only the fixed host-side post-STOP delay from 100 ms to 1.0 s:

1. `:MENU:STOP` at `2026-08-02T11:29:03.857641Z`.
2. `:WAVeform:DATA:ASCii?` at `2026-08-02T11:29:04.883651Z`.
3. `:MENU:RUN` at `2026-08-02T11:29:04.901291Z`.

It again returned zero points. The preserved raw response is exactly 11 bytes:

```text
23 39 30 30 30 30 30 30 30 30 30    #9000000000
```

Its SHA-256 is
`eae194a51647ecd4e206cf768e74036a679ed8fd1f136a5cb0879133fa66dfcd`.
The transaction took 1.129725 s, RUN was sent without transport error, and no
automatic retry or state query occurred. The local ignored session is:

`.openbench/data/captures/mho1-ascii-minimal/20260802T112903_771583Z/`

## Explicit-source retest

A separately approved third one-shot retained the 1.0 s post-STOP delay and
added only an explicit source write. It used four fresh TCP connections and the
following exact contract:

1. `:MENU:STOP` at `2026-08-02T11:35:44.862693Z`.
2. `:WAVeform:SOURce CH1` at `2026-08-02T11:35:45.870065Z`.
3. `:WAVeform:DATA:ASCii?` at `2026-08-02T11:35:45.916044Z`.
4. `:MENU:RUN` at `2026-08-02T11:35:45.970624Z`.

The interval from sending the source write to sending the data query was about
45.98 ms. The result was again the exact 11-byte zero-point response:

```text
23 39 30 30 30 30 30 30 30 30 30    #9000000000
```

Its SHA-256 is again
`eae194a51647ecd4e206cf768e74036a679ed8fd1f136a5cb0879133fa66dfcd`.
The transaction took 1.179655 s. RUN was sent without a transport error, and
there was no automatic retry, read-back, or state query. The local ignored
session is:

`.openbench/data/captures/mho1-ascii-minimal/20260802T113544_790986Z/`

## Explicit NORMAL-mode retest

The separately approved fourth one-shot retained the 1.0 s post-STOP delay and
explicit CH1 source, and added only the waveform-mode write. It used five fresh
TCP connections and the following exact contract:

1. `:MENU:STOP` at `2026-08-02T11:49:25.013149Z`.
2. `:WAVeform:SOURce CH1` at `2026-08-02T11:49:26.041001Z`.
3. `:WAVeform:MODE NORMal` at `2026-08-02T11:49:26.118441Z`.
4. `:WAVeform:DATA:ASCii?` at `2026-08-02T11:49:26.148760Z`.
5. `:MENU:RUN` at `2026-08-02T11:49:26.319320Z`.

This transaction succeeded on its only attempt. The response header was
`#9000001375`; the probe read and parsed exactly 1,375 voltage values. The
range was -2.50998 V to +2.53471 V. The raw artifact is 22,008 bytes with
SHA-256
`0c2e7e48e558eb3af3e507d45b5cc93be694cda22de4d53148678e01cba68fa0`.
The CSV artifact is 28,319 bytes with SHA-256
`3c034b586df3a1d169cb5692df2cde5fcf66cbcc2ecb9ec60db4a7dfef77506f`.

The complete transaction took 1.431840 s. There was no capture or RUN
transport error, no automatic retry, and no read-back or state query. No active
TCP connection to the oscilloscope remained after completion. The local
ignored session is:

`.openbench/data/captures/mho1-ascii-minimal/20260802T114924_887497Z/`

## Documented source grammar

Section 3.2.15.1 of the January 2026 Micsig SCPI Commands Manual defines
`:WAVeform:SOURce <source>` as a single discrete selection from
`{CH1|CH2|CH3|CH4}`. It documents neither `ALL` nor a comma-separated channel
list. The manual applies explicitly to the MHO1 series. Therefore the approved
fallback for the sixth experiment was to omit the SOURCE command entirely.

Manual:
https://manuals.plus/m/f84fe8260d5ce892f409cb4c5cc9b67b30ba63d3989c8bad073bfe9f576d8315_optim.pdf

## Zero-delay timing test

The approved fifth one-shot removed the post-STOP delay while retaining the
otherwise successful source and NORMAL-mode preparation:

1. `:MENU:STOP` at `2026-08-02T14:46:09.291257Z`.
2. `:WAVeform:SOURce CH1` at `2026-08-02T14:46:09.296458Z`.
3. `:WAVeform:MODE NORMal` at `2026-08-02T14:46:09.321339Z`.
4. `:WAVeform:DATA:ASCii?` at `2026-08-02T14:46:09.336745Z`.
5. `:MENU:RUN` at `2026-08-02T14:46:09.446414Z`.

It succeeded on its only attempt and returned exactly 1,375 points. The full
STOP-through-RUN transaction took 178.534 ms. Opening the ASCII connection,
sending the query, and receiving the complete block took 105.948 ms. The pure
interval from sending `DATA:ASCii?` through receipt of the final waveform byte
was 90.580 ms. Command-connection overhead naturally left 45.486 ms between
completion of STOP and transmission of the ASCII query even though the
configured delay was exactly zero.

The response header was `#9000001375`, the raw artifact was 22,008 bytes, and
the voltage range was -2.52235 V to +2.53471 V. RUN was sent without a transport
error. The local ignored session is:

`.openbench/data/captures/mho1-ascii-minimal/20260802T144609_267905Z/`

## No-source test

The approved sixth one-shot omitted SOURCE entirely. To isolate that change,
it restored the 1.0 s post-STOP delay and retained NORMAL mode:

1. `:MENU:STOP` at `2026-08-02T14:46:51.284234Z`.
2. `:WAVeform:MODE NORMal` at `2026-08-02T14:46:52.292518Z`.
3. `:WAVeform:DATA:ASCii?` at `2026-08-02T14:46:52.298750Z`.
4. `:MENU:RUN` at `2026-08-02T14:46:52.363304Z`.

It also succeeded on its only attempt and returned exactly 1,375 points with
header `#9000001375`. The full transaction took 1.089563 s; the pure ASCII
transfer took 32.397 ms. The raw artifact was 22,008 bytes and the voltage
range was -2.54708 V to +2.53471 V. RUN was sent without a transport error. The
local ignored session is:

`.openbench/data/captures/mho1-ascii-minimal/20260802T144651_273769Z/`

## Four-channel sequential test

The first zero-delay four-channel attempt used one STOP and one RUN, selected
CH1 through CH4 in order, and wrote NORMAL mode only once after selecting CH1.
CH1 returned 1,375 points, but CH2, CH3, and CH4 each returned the exact
zero-point block `#9000000000`. The full transaction took 414.390 ms. The local
ignored diagnostic session is:

`.openbench/data/captures/mho1-ascii-four-channel/20260802T145439_388913Z/`

The corrected attempt changed only one behavior: it repeated
`:WAVeform:MODE NORMal` after every `:WAVeform:SOURce CHn`. Its exact repeated
channel operation was:

```text
:WAVeform:SOURce CHn
:WAVeform:MODE NORMal
:WAVeform:DATA:ASCii?
```

There was still no fixed delay. One STOP preceded the four channel operations,
and one unconditional RUN followed them. All four channels succeeded on the
single attempt:

| Source | Points | ASCII transfer | Channel sequence | Voltage range |
| --- | ---: | ---: | ---: | ---: |
| CH1 | 1,375 | 26.884 ms | 135.762 ms | -2.53471 to +2.53471 V |
| CH2 | 1,375 | 60.777 ms | 140.644 ms | -2.55678 to +2.50713 V |
| CH3 | 1,375 | 57.261 ms | 113.759 ms | +0.00017712 to +0.00273270 V |
| CH4 | 1,375 | 30.034 ms | 122.166 ms | -0.00103380 to +0.00146250 V |

The four-channel read phase took 512.434 ms. The complete STOP-through-RUN
transaction took 550.632 ms, while the sum of the four pure ASCII transfer
intervals was 174.956 ms. Each raw block was 22,008 bytes with header
`#9000001375`. RUN was sent without a transport error, no automatic retry or
state query occurred, and no active oscilloscope connection remained. The
successful local ignored session is:

`.openbench/data/captures/mho1-ascii-four-channel/20260802T145540_642121Z/`

## Four-channel test with one common preamble

The next approved one-shot added exactly one
`:WAVeform:PREamble?` after `SOURCE CH1` and `MODE NORMal`, before the CH1
ASCII query. CH2-CH4 retained the successful `SOURCE`, `MODE`, `DATA` sequence.
There was no fixed delay, setting read-back, unrelated state query, or retry.

The preamble query returned these nine fields:

```text
0,0,1,4.0E-10,7.5E-9,0.0,0.012364466666666666,2.782005,0.0
```

They decode as WORD format state, NORMAL mode, count 1, X increment 0.4 ns,
X origin 7.5 ns, X reference 0, followed by the CH1-specific Y increment,
origin, and reference. Only the X calibration was shared across the four
synchronous channels; the direct ASCII values were already in volts.

All four channels again returned exactly 1,375 values. Their CSV files contain
`sample_index,time_s,<channel>_v`. The common time axis starts at 7.5 ns, ends
at 557.1 ns, and has a 0.4 ns increment. The 549.6 ns first-to-last span agrees
with the operator's 50 ns/div setting across approximately 11 horizontal
divisions, allowing for one sample interval at the boundary.

The complete STOP-through-RUN transaction took 669.258 ms. The four-channel
read phase took 599.665 ms, and the sum of the pure ASCII transfer intervals
was 218.562 ms. The PREamble connection, query, and response took 44.503 ms;
the interval from sending PREamble to receiving its complete response was
13.170 ms. The preceding comparable run without PREamble took 550.632 ms, but
the 118.626 ms whole-run difference also includes ordinary run-to-run network
and waveform-transfer variation and is not the isolated PREamble cost.

RUN was sent without a transport error. After a brief transient socket state,
all remaining local sockets were TIME_WAIT with no owning process. The local
ignored session is:

`.openbench/data/captures/mho1-ascii-four-channel/20260802T150610_805210Z/`

## Combined waveform, screenshot, and measurement test

The final approved session combined all proven operations in one STOP/RUN
transaction. It used no fixed delay after STOP and no state query or read-back.
After reading the four channels and the one common preamble, it sent
`:MEASure:CLEar all` as a separate command, opened these ten slots, requested a
direct command screenshot, and then queried the same ten scalar measurements:

```text
CH1: AMP, PKPK, RMS, FREQ, MAX
CH2: AMP, PKPK, RMS, FREQ, MIN
```

The measurement setup retained the earlier proven pacing: 100 ms after CLEAR,
250 ms after each OPEN, and 500 ms after the final OPEN. The screenshot reader
permits one bounded repeat only when the first `:SYS:SCR?` response is empty or
invalid, with at least 1.0 s between the two commands. A preceding diagnostic
run exercised the failure case: all waveforms and all ten measurements
succeeded, while the only screenshot query returned the valid empty block
`#10`. That session took 4.539313 s and is preserved at:

`.openbench/data/captures/mho1-ascii-four-channel/20260802T151657_776086Z/`

The complete rerun succeeded without needing the conditional repeat. It sent
exactly 37 commands, including one screenshot query, and produced these scalar
values:

| Source | Measurement | Value |
| --- | --- | ---: |
| CH1 | AMP | 5.007608891 V |
| CH1 | PKPK | 5.069431305 V |
| CH1 | RMS | 1.763500333 V |
| CH1 | FREQ | 10.016026 MHz |
| CH1 | MAX | 2.509986639 V |
| CH2 | AMP | 5.051512241 V |
| CH2 | PKPK | 5.063923836 V |
| CH2 | RMS | 1.759745955 V |
| CH2 | FREQ | 9.992006 MHz |
| CH2 | MIN | -2.576172829 V |

All four waveform queries returned 1,375 points. The common X calibration was
again 0.4 ns per sample with 7.5 ns origin, giving a 7.5 ns through 557.1 ns
time axis. The complete transaction took 4.722938 s, divided as follows:

| Phase | Time |
| --- | ---: |
| Four-channel read, including preamble and command overhead | 522.753 ms |
| Sum of four ASCII payload transfers | 224.816 ms |
| Preamble connection/query/response | 30.296 ms |
| Measurement clear/open/stabilization | 3.531454 s |
| Screenshot phase | 118.575 ms |
| Ten scalar measurement queries | 437.617 ms |

The direct `:SYS:SCR?` block contained 160,268 payload bytes. The instrument's
payload had its known malformed JFIF marker (`FF D8 58 00 00 10 JFIF`); the
saved image repaired only that marker to `FF D8 FF E0 00 10 JFIF`. The resulting
JPEG was decoded and visually inspected at 1280 x 800 pixels. Its SHA-256 is
`1edbb0b03b02c5388d51f37c1bcfbca9448c5edc58bcda76185fa48dd03c7b8a`.
The measurement CSV SHA-256 is
`9b40a8bc030dfb2ffffaa7d0d875373ec259a35b7f2a412a3ab523ec65c3967c`.

No capture, preamble, screenshot, measurement, or RUN error occurred. The final
sent command was `:MENU:RUN`, and the actual 37-command sequence matched the
recorded contract exactly. The successful local ignored session is:

`.openbench/data/captures/mho1-ascii-four-channel/20260802T152125_962547Z/`

### Uniform 100 ms measurement-pacing retest

The full combined test was repeated after changing all three measurement setup
delays to 100 ms: after CLEAR, after every OPEN, and after the final OPEN. The
fixed setup delay therefore fell from 3.1 s to 1.2 s. No other command or
capture behavior changed.

The complete transaction from immediately before sending STOP through
completion of RUN took 2.973638 s. Measurement configuration took 1.450567 s;
the remainder was 1.523071 s in this run. The interval from completion of STOP
through completion of RUN was 2.940632 s.

All four channels returned 1,375 points, all ten scalar measurements were
available, and the direct screenshot succeeded on its first attempt with a
159,624-byte payload. The screenshot was decoded and visually inspected. The
37-command sequence ended in `:MENU:RUN`; no capture, preamble, screenshot,
measurement, or RUN error occurred. The local ignored session is:

`.openbench/data/captures/mho1-ascii-four-channel/20260802T152735_868128Z/`

## Ten consecutive preconfigured frames

The ten measurement slots were configured once before the timed series using
the uniform 100 ms pacing. Each subsequent frame contained exactly 26 commands:
one STOP, four sequential SOURCE/MODE/ASCII channel operations with one common
preamble, one direct screenshot, ten scalar measurement queries, and one final
RUN. No frame contained CLEAR or OPEN. The measured duration began immediately
before sending STOP and ended after sending RUN.

An initial zero-dwell series left only 33-96 ms in RUN between frames. Although
all ten frame times were below 1.167 s, frames 2-10 returned zero points for CH1
and sometimes CH2, so the series was invalid. A 250 ms RUN acquisition interval
was therefore added between frames; it is outside the timed STOP-to-RUN window.

The first 250 ms series made all ten frames valid, but one isolated CH2 command
sequence stalled for about 1.15 s. That frame took 2.072785 s, exceeding the
2.0 s limit by 72.785 ms; the other nine passed. An unchanged control series
then passed the complete criterion:

| Frame | STOP-to-RUN time |
| ---: | ---: |
| 1 | 0.970136 s |
| 2 | 1.009303 s |
| 3 | 0.941340 s |
| 4 | 0.869034 s |
| 5 | 0.834016 s |
| 6 | 1.079901 s |
| 7 | 0.917800 s |
| 8 | 0.972996 s |
| 9 | 0.948786 s |
| 10 | 1.025532 s |

The minimum was 0.834016 s, maximum 1.079901 s, mean 0.956884 s, and median
0.959461 s. Every frame returned 1,375 points on all four channels, all ten
measurements, and a valid screenshot on the first attempt. Every actual command
sequence matched its 26-command contract and ended in RUN. The one-time
measurement setup took 1.414653 s and is excluded from every frame time. The
successful batch record is:

`.openbench/data/captures/mho1-ascii-ten-frame/20260802T163823_492866Z/summary.json`

## Interpretation

The literal three-command profile is insufficient on the tested instrument in
its unqueried pre-existing waveform state at both 100 ms and 1.0 s after STOP.
The matching empty blocks make a simple STOP-settling delay an unlikely primary
cause. Explicitly selecting CH1 alone also does not populate the fast-ASCII
response. Removing the OpenBench skill, API, background polling, measurements,
screenshots, and all state/read-back commands does not by itself make a fresh
fast-ASCII query return samples.

This does not show that fast ASCII is unsupported. Earlier recorded tests on
the same instrument returned 1,100 and 5,500 numeric voltage samples after
explicit waveform-source/mode preparation. The successful fourth experiment
isolates `:WAVeform:MODE NORMal` as the missing preparation command in this
minimal series: the otherwise identical explicit-source transaction returned
zero points, while adding only NORMAL mode returned 1,375 valid values.

The fifth experiment shows that an explicit host-side STOP delay is not needed
when the intervening SOURCE and MODE writes are present. The sixth shows that a
SOURCE write is not needed when the oscilloscope already retains a suitable
current source. It does not prove that SOURCE can always be omitted from an
unknown initial state; SOURCE is a persistent single-channel selection.

The two removals have only been demonstrated independently. The successful
zero-delay contract still included SOURCE, while the successful no-SOURCE
contract still included the 1.0 s delay. Therefore this series does not yet
claim that both can be removed simultaneously. Neither of those two minimal
successful tests needed a waveform preamble, setting query, read-back, scalar
measurement, screenshot, retry, OpenBench API, or OpenBench skill. The later
combined test deliberately added the preamble, ten scalar measurements, and
direct screenshot while retaining the same zero-delay four-channel waveform
sequence.

For sequential multi-channel acquisition on firmware 2.154.75, NORMAL mode
must be refreshed after each source selection in the tested command pattern.
Writing it only once allowed CH1 to succeed but left CH2-CH4 empty; repeating
the same MODE write after every SOURCE made all four channels succeed without
any fixed delay.

## OpenBench production integration contract

The accepted production implementation uses the successful control-series
contract and excludes binary waveform acquisition:

1. Settings exposes ten profile rows. One Apply operation sends CLEAR, waits
   100 ms, sends every selected OPEN with a 100 ms pause, then waits a final
   100 ms. This configuration is not timed as part of a frame.
2. Every poll reads the already configured scalar measurements. Screenshot and
   ASCII data are independent options, and ASCII selects any subset of CH1-CH4.
3. When ASCII data is enabled, the frame sends STOP, only the selected
   SOURCE/MODE NORMAL/ASCII groups with one common preamble, an optional direct
   screenshot query, the scalar queries, and RUN in `finally`. With ASCII data
   disabled it sends no STOP/RUN and reads only the optional screenshot plus
   scalars. No path sends a status/read-back query, measurement CLEAR/OPEN, or a
   fixed post-STOP delay. A failed first screenshot response permits only one
   paced direct retry; there is no scope-side file fallback.
4. Dashboard polling defaults to 2.0 s and rejects smaller values. Independently
   of that start-to-start scheduler, OpenBench preserves at least 250 ms of RUN
   acquisition time after one completed waveform frame before another waveform
   frame may begin.
5. Snapshot Screen and Data artifacts come from the same poll. Data contains
   only the selected ASCII channels and is never reconstructed from pixels.

The two-second setting is a minimum polling interval, not a guarantee that all
physical transfers finish within two seconds: the earlier isolated 2.072785 s
CH2 stall remains part of this record. RUN restoration and the additional
acquisition dwell make the next frame safe even after such an outlier.
