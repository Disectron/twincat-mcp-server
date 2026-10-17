# Beckhoff ADS (AdsLib) — vendored

Source: https://github.com/Beckhoff/ADS, commit 24e598a113edfdc0d34414bfefb847930d94cb87 (2026-09-17).
License: MIT (LICENSE in this folder). Copied unmodified: `AdsLib/` (only `AdsLib/CMakeLists.txt` left out —
`native/capture/CMakeLists.txt` lists the sources it compiles).

Used by tcmcp-capture:
- `AdsDevice` / `AdsNotification` (C++ object interface) for connecting and subscribing notification blocks;
- TwinCAT mode (`USE_TWINCAT_ROUTER`, `AdsLib/TwinCAT/AdsLib.cpp`) on Windows: every call goes to Beckhoff's
  TcAdsDll.dll — the TwinCAT router itself;
- standalone mode (`AdsLib/standalone/*`) elsewhere: AdsLib's own AMS/TCP router to a remote target.
To update: replace `AdsLib/` with a newer release, keep this note's commit line current, rebuild, run the tests.
