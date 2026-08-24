# Contributing to OpenBench

Start with:

- [Architecture](docs/architecture.md)
- [Driver development](docs/driver-development.md)
- [Reproducibility and portability](docs/portability.md)
- [Automation API](docs/automation-api.md)

On Windows, prepare a development environment with:

```powershell
scripts\setup-openbench.ps1 -Dev
```

Before proposing a change, run:

```powershell
ruff check .
mypy src
pytest -q
```

The Dashboard is bilingual. Keep English as the source text and add the Russian
equivalent to `src/openbench/web/static/i18n.js` in the same change. Verify both
languages for safety-critical controls and any text inserted dynamically by
HTMX or JavaScript.

When an API endpoint or safety rule changes, update
`skills/openbench/SKILL.md`, its compact API reference/helper, and
`docs/automation-api.md` together. Validate the skill package before commit.

Hardware-affecting changes also require a bounded live test with known-safe
wiring/load and an updated dated record under `docs/`. Never include personal
captures, databases, instrument comments, serial numbers that should remain
private, or extracted proprietary firmware in a change.

Mutable runtime data belongs only under `.openbench/`. Public live-test records
must replace serial numbers, MAC addresses, private bench IP addresses, COM
assignments, user paths, and operator comments with clearly marked examples.

Contributions are accepted under the repository's MIT License. Third-party
components retain their own licenses as listed in `THIRD_PARTY_NOTICES.md` and
generated runtime manifests.
