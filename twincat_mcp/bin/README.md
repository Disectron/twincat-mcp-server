# Native capture service

`tcmcp-capture.exe` — built by CI (`.github/workflows/ci.yml`, job `native`) from `native/capture`
(Beckhoff AdsLib on TcAdsDll). The server uses it for cycle-mode recordings when its `--version` equals the
package version; otherwise recordings run on the Python recorder and the response says why
(`engine_note`). Build it yourself: see `native/capture/README.md`.
