# tcmcp-capture — native capture service

The time-critical part of twincat-mcp cycle-mode recordings (`rec_start` / `rec_capture`, `engine` native):

```
PLC (TwinCAT RT) ──ADS notifications──▶ tcmcp-capture (C++)                ◀── start/status/stop (JSON lines) ── MCP server (Python)
                                         ├ Beckhoff AdsLib AdsNotification → callback: copy bytes + PLC FILETIME into a lock-free ring
                                         ├ writer thread: merge blocks by PLC time, recording format 2, SHA-256 + HMAC footer
                                         └ accounting: head / gaps / tail / overflow per block, verdict hint
```

* **ADS:** Beckhoff's open-source **AdsLib** (`third_party/beckhoff-ads`, MIT): `AdsDevice` + `AdsNotification`.
  On Windows it runs in TwinCAT mode, i.e. over Beckhoff's **TcAdsDll.dll** (the TwinCAT router, same path as
  TwinCAT's own tools). The exe binds TcAdsDll.dll at start (`TWINCAT3DIR\..\Common64`, `C:\TwinCAT\Common64`, PATH),
  so it builds without the TwinCAT SDK; `-DTCMCP_TCADSDLL_SDK=<…\AdsApi\TcAdsDll>` builds against Beckhoff's own
  headers and import library instead. Elsewhere: AdsLib standalone (own AMS/TCP router, remote targets).
* **Hot path:** the callback copies the sample into a pre-allocated single-producer/single-consumer ring and returns.
  No allocation, lock, I/O or decoding. A full ring drops the new sample and counts it (`overflow_samples`,
  intervals in the footer) — reported, never silent, verdict FAIL.
* **File:** recording format 2 exactly as the Python recorder writes it (header from the server, `_t0`, rows
  `[t_ms, "#block", base64]` with exact 4-decimal PLC-relative times, footer with `sha256` over every line before it
  and `hmac` with the install key). `rec_verify`, `rec_read`, `rec_export`, `rec_stats` read it unchanged.
* **Window accounting:** duration counts from the moment every block delivers; `missing_head` (pre-trigger buffer
  not full), `missing_gaps` (holes in the PLC timestamps, incl. overflow), `missing_tail` (stream ended before the
  window end: PLC stopped). `requested_s`, `plc_span_s`, `window_start_ms`, `window_end_ms`, `completeness`,
  `verdict_hint` in the footer.
* **All or nothing:** if any block cannot be subscribed, everything is unsubscribed, no file is created and the
  error names the block (igrp/ioffs/size, ADS code).
* **One process per recording.** Closing stdin ends the recording cleanly (signed footer) and exits.

## Build

```
cmake -S native/capture -B build/capture -DCMAKE_BUILD_TYPE=Release
cmake --build build/capture --config Release
build/capture/Release/tcmcp-capture.exe --version     # must print the package version
build/capture/Release/tcmcp-capture.exe --selftest
```

Windows: Visual Studio 2019+ (C++ desktop workload) or Build Tools, CMake ≥ 3.16. With the TwinCAT SDK headers:
`-DTCMCP_TCADSDLL_SDK="C:/Program Files (x86)/Beckhoff/TwinCAT/AdsApi/TcAdsDll"` (checks the same struct
layouts at compile time). CI builds the exe on every push (artifact `tcmcp-capture-windows-x64`); the release
copy lives in `twincat_mcp/bin/`.

## Control protocol (protocol 1)

| → | ← |
|---|---|
| `{"cmd":"start","backend":"ads","ams_net_id","port","file","header","blocks":[{id,igrp,ioffs,size,leaves}],"cycle_100ns","max_delay_100ns","duration_s","post_trigger_s","pre_trigger_ms","max_events","start_when","stop_when","hmac_key_path","ring_s","ring_mb"}` | `{"reply":"start","ok":true,"subscribed":N,"source":{…},"ring":{…}}` or `{"ok":false,"error","block","code"}` |
| `{"cmd":"status"}` | samples, events, missing_*, overflow_samples, backlog, max_backlog, plc_span_s, per-block counters |
| `{"cmd":"stop"}` | `{"reply":"stop","ok":true,"footer":{…}}` |
| — | `{"event":"finished","footer":{…}}` when duration / post-trigger / stop_when / max_events ended it |

Conditions arrive resolved to raw bytes: `{"block","off","type":"u32|i16|f64|bool|…","op","value","edge"}`.
`"backend":"sim"` replaces ADS with a simulated PLC (UDINT counters, exact timestamps, injectable drops / late
blocks / PLC stop / subscription failure) — used by `tests/test_native_capture.py` and CI.
