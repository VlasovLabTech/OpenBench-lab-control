## Summary

Describe what changed and why.

## Verification

- [ ] `ruff check .`
- [ ] `mypy src`
- [ ] `pytest -q`
- [ ] Relevant package, portability, or hardware checks were run.

## Project requirements

- [ ] New or changed Dashboard text is available in both English and Russian.
- [ ] API changes include matching documentation and OpenBench skill updates.
- [ ] Hardware-affecting changes use bounded commands and document a safe test.
- [ ] No credentials, private paths, serial numbers, captures, comments, or
      proprietary firmware are included.
- [ ] Third-party sources, checksums, patches, and licenses are recorded when needed.

## Hardware testing

State whether physical hardware was used. If it was, identify only the public model
and firmware profile, summarize the known-safe wiring/load, and link the dated test
record. Never include a serial number or private bench address.
