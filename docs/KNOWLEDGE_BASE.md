# Knowledge base — the ground rules

Agreed with Ryan on 2026-10-18 (0.8.11). Permanent: a change to any rule below needs his approval and is recorded
here. Code that touches the knowledge folder follows these rules; `twincat_mcp/kb_layout.py` is the one place that
names its paths.

## 1. Layout and ownership

```
<kb>\                      the folder chosen once with kb_set_location (default D:\TwinCAT-MCP\knowledge)
  README.md                orientation
  manuals\                 Ryan's: PDF manuals, any sub-folders — read, never written or deleted
  sop\                     Ryan's: notes and SOPs (.md) — read, never written or deleted
  tools\                   builder stubs; a file is replaced only when it is one the plugin shipped (sha256),
                           anything else there (his own scripts) is left alone
  index\                   the plugin's; every file rebuildable from its source
    infosys-<lcid>.db      Beckhoff Information System (the local Help Viewer package)
    manuals.db             the PDFs in manuals\
    knowledge.db           project folders added with kb_add_project, ESI devices
    README.md              what each index is, its format, how it is rebuilt
```

These names are final. The move into this layout happens once (0.8.11): an index found in an older place
(`infosys\helpviewer-<lcid>\index.sqlite`, `manualsdb\manuals.db`, `knowledge.db` at the top) is renamed into
`index\` — same drive, instant, never copied, never over an existing file; a file in use is moved at a later start.

## 2. One index per source

| Source | Index | Built by |
|---|---|---|
| the Help Viewer package on this PC (`BKINFOSYS3_*.mshc`, opened by Help Viewer v2.3 and the Beckhoff Information System shortcut) | `index\infosys-<lcid>.db` | `tools\infosys_local.py index` (the plugin's own extraction) |
| the PDFs in `manuals\` | `index\manuals.db` | `tools\manuals_db.py update` |
| project folders added by Ryan, TwinCAT's ESI folder | `index\knowledge.db` | `kb_add_project` / `kb_reindex`, the ESI index |

Every reader uses that one index: the `infosys_*` tools and the twincat-docs skill read the same
`infosys-<lcid>.db`. A second index of the same source is not built again.

## 3. Sources are local

The Help Viewer package found on this PC (or `[docs] mshc`). Nothing is fetched from the network unless
`docs.source = 'web'` is set; that web cache lives outside the knowledge folder and is not an index of it.

## 4. Normalise once, when indexing

Text is stored the way every reader needs it: plain spaces (no non-breaking spaces), no invisible marks, no image
placeholders or empty links, table cells joined where the page's own code shows the name was split, help links
resolved to page addresses. Readers never repair stored data; a fix to what is stored is a format change (rule 5).

## 5. Format contract

- Every index records in its `meta` table: its format version, the stamp of the source it was built from, the
  version of the plugin that built it, and its state.
- Adding a table or a column needs no new format version; changing or removing one does.
- A test fingerprints the code that produces each format and fails when that code changes without a new format
  version (Infosys extraction, manuals text, ESI parser).
- A reader reads an index of an older format as it is and says that a rebuild is due.

## 6. Rebuilds

- **The first build** of an index that does not exist yet starts by itself (there is nothing to keep).
- **A new format, or a changed source** (a new Help Viewer package, new or changed PDFs, new ESI files): the index
  is reported as `rebuild_needed` / `outdated`, with the reason, in `kb_status`, `tc_doctor` and every
  documentation answer — and **stays in use as it is until Ryan approves the rebuild** (`kb_rebuild`, which asks
  him, or the builder command).
- How an index changes, so that a reader sees the old index or the new one, never half of one:
  - `infosys-<lcid>.db`, `manuals.db`: the build writes `<index>.building` beside the index (the manuals update
    starts from a copy of the current one) and replaces the index only when the new one is complete. An
    interrupted build never replaces anything and is reported as interrupted; the next Infosys build resumes it,
    the next manuals update starts again from the index in use (PDFs already in it are not read again).
  - `knowledge.db` also holds the project folders, so it is never swapped as a file: the ESI part changes in one
    transaction (the database is WAL — readers keep the rows of before until the commit; an error part-way changes
    nothing).
- At most one format change per index per release, and only when stored content changes.

## 7. Nothing is deleted by the plugin

Files the plugin no longer reads (an old second index, an old copy left beside a moved one) are listed by
`kb_status` with their size; Ryan deletes them.

What the plugin itself does replace, and nothing else:
- an index, by its complete rebuild — the first build, or one Ryan approved (rule 6);
- its own `<index>.building` and `<index>.lock`: a build resumes or replaces the `.building` of an interrupted
  build, and the lock of a build that is gone;
- a builder copy in `tools\` that the plugin shipped, by the stub (the old file is kept as `<name>.v<version>.bak`);
- an index in an old place, moved into `index\` once (rule 1) — a rename, nothing is copied or lost.

## 8. Where the rules live

Here, and in `index\README.md` (written by the plugin, rewritten when this text changes). The folder's own
`README.md` is Ryan's.
