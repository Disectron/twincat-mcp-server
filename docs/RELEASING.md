# Releasing twincat-mcp

The plugin installs from `main`, and Claude Code caches it by version. A version on `main` must therefore never
exist without its matching `tcmcp-capture.exe` (field report N25). The release workflow
(`.github/workflows/release.yml`) guarantees that.

## Flow

1. Work lands on `main` with the version of the **last release** (no bump on `main`). The exe in
   `twincat_mcp/bin/` keeps matching that version, so the native engine keeps working for anyone on `main`.
2. Cut the release from `main`:
   ```
   git checkout -b release/vX.Y.Z
   # bump: twincat_mcp/__init__.py, pyproject.toml, .claude-plugin/plugin.json, .claude-plugin/marketplace.json,
   #       docs/INSTALL.md (plugin cache paths), python scripts/gen_tools_md.py
   # README.md changelog: a **X.Y.Z** entry (the workflow refuses to run without it)
   git push -u origin release/vX.Y.Z
   ```
3. The workflow, on that branch:
   - checks that branch, package, plugin and marketplace versions agree and that the changelog has the entry;
   - builds `tcmcp-capture.exe` on Windows (Release, `/Brepro`), runs `--version` and `--selftest`, then the whole
     test suite against it;
   - commits the exe and `tcmcp-capture.exe.sha256` onto the branch, tags `vX.Y.Z`, fast-forwards `main`
     (refused if `main` moved meanwhile — rebase the release branch);
   - publishes the GitHub release: `twincat-mcp-X.Y.Z-windows-x64.zip`, `twincat-mcp-X.Y.Z.mcpb`,
     `tcmcp-capture.exe`, `SHA256SUMS`, build provenance (`gh attestation verify`).
4. Afterwards, locally: `git checkout main && git pull --ff-only`, then re-run CMake configure + build
   (`cmake -S native/capture -B build/capture && cmake --build build/capture`) so the local exe has the new version.

## Kept, not cleaned up

`release/v*` branches and `v*` tags stay (decision 2026-10-18). Each release branch ends at the same commit as the
tag; `main` contains all of them.

## Released

| Version | Content | Date |
|---|---|---|
| 0.8.1 | recording authenticity (R1–R12: gap markers, label, hash checkpoints, sealed + read-only files, export provenance, datatype GUIDs), 0.8.0 retest fixes, exe shipped in the plugin | 2026-10-18 |
| 0.8.2 | TwinCAT Scope: `scope_export`, `scope_list`, `scope_record`, `rec_start(also_scope)`, `[recorder] also_scope` | 2026-10-18 |
| 0.8.3 | Infosys from the local Help Viewer package (`docs.source = "local"`, no network) | 2026-10-18 |
