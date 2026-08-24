# Third-party notices

OpenBench uses and redistributes the following third-party component in its web
package:

## htmx 2.0.4

- Project: <https://github.com/bigskysoftware/htmx>
- Distributed file: `src/openbench/web/static/htmx-2.0.4.min.js`
- Upstream SHA-256: `E209DDA5C8235479F3166DEFC7750E1DBCD5A5C1808B7792FC2E6733768FB447`
- License: Zero-Clause BSD, preserved in
  `src/openbench/web/static/htmx-LICENSE.txt`

The optional Kingst sigrok runtime is built from the pinned upstream sources
recorded in `scripts/kingst-runtime.lock.json`. Its generated package includes
the corresponding libsigrok and sigrok-cli license files. Proprietary Kingst
firmware is not stored in this repository; the setup script extracts it from
the official vendor package on the user's machine.
