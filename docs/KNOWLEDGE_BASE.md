# Knowledge base — the ground rules

Agreed with Ryan on 2026-10-18 (0.8.11). Permanent: a change to any rule below needs his approval and is recorded
here. Changes: 2026-10-19 (0.8.12, Ryan asked for an index of the ESI files he put in `manuals\Beckhoff_EtherCAT_XML`)
— the ESI devices have their own index, `index\esi.db`, built from the ESI files in `manuals\` and TwinCAT's ESI
folders; `knowledge.db` keeps the project folders only (rules 1, 2, 5, 6). 2026-10-19 (0.8.12, the 0.8.11 retest
N102) — `knowledge.db`'s search table is derived from its stored units; a new layout of it is rebuilt from them at the
next `kb_reindex` / `kb_add_project` (rule 6). Code that touches the knowledge folder follows these rules;
`twincat_mcp/kb_layout.py` is the one place that names its paths.

## 1. Layout and ownership

```
<kb>\                      the folder chosen once with kb_set_location (default D:\TwinCAT-MCP\knowledge)
  README.md                orientation
  manuals\                 Ryan's: PDF manuals and ESI files (.xml), any sub-folders — read, never written or deleted
  sop\                     Ryan's: notes and SOPs (.md) — read, never written or deleted
  tools\                   builder stubs; a file is replaced only when it is one the plugin shipped (sha256),
                           anything else there (his own scripts) is left alone
  index\                   the plugin's; every file rebuildable from its source
    infosys-<lcid>.db      Beckhoff Information System (the local Help Viewer package)
    manuals.db             the PDFs in manuals\
    esi.db                 the EtherCAT device descriptions: the ESI files in manuals\ and TwinCAT's ESI folders
    knowledge.db           project folders added with kb_add_project
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
| the ESI files (`.xml` whose root is EtherCATInfo / EtherCATModule / EtherCATDict / EtherCATDiag) in `manuals\`, TwinCAT's `Config\Io\EtherCAT` and `knowledge.esi_dirs` | `index\esi.db` | the plugin's `esi_db.py update` (`twincat_mcp/esi_index.py`) |
| project folders added by Ryan | `index\knowledge.db` | `kb_add_project` / `kb_reindex` |

Every reader uses that one index: the `infosys_*` tools and the twincat-docs skill read the same
`infosys-<lcid>.db`, the `hw_*` tools and the skill's `hw_*` commands the same `esi.db`. A second index of the same source is not built again.

## 3. Sources are local

The Help Viewer package found on this PC (or `[docs] mshc`). Nothing is fetched from the network unless
`docs.source = 'web'` is set; that web cache lives outside the knowledge folder and is not an index of it.

## 4. Normalise once, when indexing

Text is stored the way every reader needs it: plain spaces (no non-breaking spaces), no invisible marks, no image
placeholders or empty links, table cells joined where the page's own code shows the name was split, help links
resolved to page addresses. Readers never repair stored data; a fix to what is stored is a format change (rule 5).

## 5. Format contract

- Every index records its format version, what it was built from and who built it (builder and plugin version):
  `infosys-<lcid>.db` the package's content stamp and file; `manuals.db` each PDF's path, size, modification time
  and SHA-256; `esi.db` every ESI file's path, size, modification time and content hash, and the folders it read.
  An index a build can leave half-done also records its state.
- Adding a table or a column needs no new format version; changing or removing one does.
- A test fingerprints the code that produces each format and fails when that code changes without a new format
  version (Infosys extraction, manuals text, ESI index).
- A reader reads an index of an older format as it is and says that a rebuild is due.

## 6. Rebuilds

- **The first build** of an index that does not exist yet starts by itself (there is nothing to keep): Infosys at
  server start or when a knowledge folder is chosen, manuals at server start when `manuals\` holds PDFs and the
  server's Python can read them, ESI at server start (or the first `hw_*` call) when an ESI file is there.
- **One build at a time**: a build holds an operating-system lock on `<index>.lock` for its whole life; the
  operating system releases it however the build ends, so whether a build runs is known, never guessed.
- **A new format, or a changed source** (a new Help Viewer package, new or changed PDFs, new ESI files): the index
  is reported as `rebuild_needed` / `outdated`, with the reason, in `kb_status`, `tc_doctor` and every
  documentation answer — and **stays in use as it is until Ryan approves the rebuild** (`kb_rebuild`, which asks
  him, or the builder command).
- How an index changes, so that a reader sees the old index or the new one, never half of one:
  - `infosys-<lcid>.db`, `manuals.db`, `esi.db`: the build writes `<index>.building` beside the index (the manuals update
    starts from a copy of the current one) and replaces the index only when the new one is complete. An
    interrupted build never replaces anything and is reported as interrupted; the next Infosys build resumes it,
    the next manuals update starts again from the index in use (PDFs already in it are not read again), the next
    ESI build starts over (minutes).
  - `knowledge.db` holds the project folders, so it is never swapped as a file: it changes in one transaction per
    folder (the database is WAL — readers keep the rows of before until the commit); a folder being added is
    registered in the transaction that stores its units, or not at all. One change at a time: `kb_add_project`,
    `kb_reindex`, `kb_remove_project` hold the lock on `knowledge.db.lock` (as a build holds `<index>.lock`); a
    second one is refused with what is running. A read (`kb_status`, `kb_search`) never creates `index\` or
    `knowledge.db`. Its search table (`units_fts`)
    is derived from the stored units: a new layout of it (0.8.12: the words of names and identifiers, the code
    without comments) is rebuilt from them in one transaction at the next `kb_reindex` / `kb_add_project` — no
    project file is read for it; until then `kb_search` uses the table as it is and says so. The ESI table it held
    up to 0.8.11 is no longer read; `kb_status` lists it, the plugin never drops it.
- At most one format change per index per release, and only when stored content changes.

## 7. Nothing is deleted by the plugin

Files the plugin no longer reads (an old second index, an old copy left beside a moved one) are listed by
`kb_status` with their size; Ryan deletes them.

What the plugin itself does replace, and nothing else:
- an index, by its complete rebuild — the first build, or one Ryan approved (rule 6);
- its own `<index>.building`: a build resumes the `.building` of an interrupted build of the same format and
  source, and discards one of another format or source (or one left next to a complete, current index);
  `<index>.lock` stays (empty when no build runs);
- a builder copy in `tools\` that the plugin shipped, by the stub (the old file is kept as `<name>.v<version>.bak`);
- an index in an old place, moved into `index\` once (rule 1) — a rename, nothing is copied or lost.

## 8. Where the rules live

Here, and in `index\README.md` (written by the plugin, rewritten when this text changes). The folder's own
`README.md` is Ryan's.
