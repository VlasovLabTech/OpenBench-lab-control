# Security policy

## Reporting a vulnerability

Please use GitHub private vulnerability reporting for the repository. Do not
open a public issue containing credentials, private capture data, instrument
serial numbers, network details, or a procedure that could bypass an OpenBench
safety interlock.

Include the affected version or commit, the smallest safe reproduction, and the
expected impact. Do not energize laboratory outputs or test high-energy hardware
solely to reproduce a software report.

## Deployment boundary

OpenBench is a local engineering tool. It binds to `127.0.0.1` by default and
does not provide authentication or TLS. Do not expose it directly to an
untrusted network. Remote access requires a separately reviewed authenticated
gateway and a bench-specific safety assessment.

## Sensitive local data

`.openbench/` contains the SQLite database, capture comments, screenshots,
waveforms, logs, generated tools, audit reports, and archived local research.
The directory is ignored by Git, but it may still contain sensitive laboratory
information. Review it before sharing a workspace archive or support bundle.

Never commit credentials, extracted proprietary firmware, private keys,
operator comments, personal paths, private bench addresses, or real hardware
identifiers. Public hardware records use clearly marked example identifiers.

## Supported code

Security fixes target the repository's default branch and the most recent
published release. Older snapshots may not receive backports.
