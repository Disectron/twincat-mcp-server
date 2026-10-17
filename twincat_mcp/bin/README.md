# Native capture service

`tcmcp-capture.exe` — built from `native/capture` (Beckhoff AdsLib on TcAdsDll) by the release workflow
(`.github/workflows/release.yml`), which runs the whole test suite against it and commits it here together with
`tcmcp-capture.exe.sha256`. The server uses it for cycle-mode recordings only when its `--version` equals the
package version **and** its SHA-256 equals the pinned one; otherwise recordings run on the Python recorder and
the response says why (`engine_note`). Provenance: `gh attestation verify tcmcp-capture.exe -R
Disectron/twincat-mcp-server`. Build it yourself: see `native/capture/README.md` (a self-built exe is used via
`TCMCP_CAPTURE_EXE` or `[recorder] capture_exe`; the pin applies to the exe in this folder only).
