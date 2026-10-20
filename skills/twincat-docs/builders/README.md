# Builders: infosys_local.py, manuals_db.py and esi_db.py

Three scripts that build the knowledge folder's indexes, read by the `twincat-docs` skill (`kb_docs.py`) and by the
twincat MCP server. `infosys_local.py` version 3, `manuals_db.py` version 6, `esi_db.py` version 1 (plugin 0.8.12). The layout,
the states and when an index is rebuilt follow the ground rules in the plugin's `docs/KNOWLEDGE_BASE.md`; the paths
come from `twincat_mcp/kb_layout.py`, which the server, `kb_docs.py` and both builders import.

| script | reads | writes |
|---|---|---|
| `infosys_local.py` | Beckhoff Infosys package `BKINFOSYS3*.mshc` (Microsoft Help Viewer) | `<kb>\index\infosys-1033.db` (`--lang de`: `infosys-1031.db`) |
| `manuals_db.py` | PDF files under `<kb>\manuals\` (sub-folders allowed) | `<kb>\index\manuals.db` |
| `esi_db.py` | ESI files (`.xml`) under `<kb>\manuals\` and in TwinCAT's `Config\Io\EtherCAT` (+ `--esi-dir`) | `<kb>\index\esi.db` |

`infosys_local.py` runs the plugin's own extractor (`twincat_mcp.infosys_build`, from the plugin folder the builder
ships in): the one Infosys index, the same file the `infosys_*` tools read. Version 2 built a second, different index
(`helpviewer\infosys_en.db`); nothing reads it any more — `kb_status` lists it with its size for you to delete. Both
builders need the plugin folder they ship in (`twincat_mcp\` beside `skills\`); PDF text needs PyMuPDF or pypdf
(section 5).

## 1. Requirements and location

- **Python 3.12** (the one version the plugin targets; newer is accepted). Both scripts check the version first and
  refuse older versions with a message: `infosys_local.py needs Python 3.12 or newer; this is 3.11.x ...`. The files
  still compile on older Pythons so that the message is shown. Use the python.org build, for example
  `C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`.
- **Knowledge folder `<kb>`** is found in this order:
  1. `--kb PATH` on the command line,
  2. the environment variable `TCMCP_KNOWLEDGE_DIR`,
  3. the folder chosen with the plugin's `kb_set_location` (pointer file `~\.twincat_mcp\knowledge_location.json`,
     or `%TCMCP_HOME%\knowledge_location.json` when `TCMCP_HOME` is set),
  4. the folder above the script, when that folder is a knowledge folder (it contains `index\` — or, before the
     move into `index\`, `knowledge.db`, `helpviewer\` or `manualsdb\`). This is why a builder in `<kb>\tools\`
     works, and the stub there (section 6: it sets `TCMCP_STUB_DIR`, so "the script" means the stub),
  5. `D:\TwinCAT-MCP\knowledge` (the default, `FALLBACK_KB` in each script).
- `--kb` and `--db` may be given before or after the command: `--kb D:\kb index` and `index --kb D:\kb` are the same.
- In a new folder `index\` does not exist yet, so the folder-above lookup does not apply. Pass `--kb` on the first run.
- Each script has its own `--db` override: the index file itself.
- **Layout move, once.** `index` / `update` first move an index found in an older place into `index\`
  (`manualsdb\manuals.db`, `knowledge.db` at the top, `infosys\helpviewer-1033\index.sqlite`) — a rename on the
  same drive, never a copy, never over an existing file; each move is printed as `layout: …`.

## 2. infosys_local.py

### Commands

```
python infosys_local.py index  [--lang en|de] [--mshc PATH] [--kb PATH | --db PATH] [--force]
python infosys_local.py search QUERY [--book GLOB] [--limit 10]
python infosys_local.py show   REF [--max 4000] [--section NAME] [--book GLOB]
python infosys_local.py decl   SYMBOL [--book GLOB]
python infosys_local.py books  [--filter GLOB]
python infosys_local.py stats  [--json]
```

Examples:

```
python infosys_local.py --kb "D:\TwinCAT-MCP\knowledge" index
python infosys_local.py index --kb "D:\TwinCAT-MCP\knowledge" --force
python infosys_local.py search "profiler OR GETCPUACCOUNT" --book "tcplclib_*" --limit 6
python infosys_local.py decl FB_CXProfiler
python infosys_local.py show 35053195 --section Outputs --max 1500
python infosys_local.py show tcplclib_tc2_utilities/35053195
python infosys_local.py books --filter "tc3_*"
python infosys_local.py stats --json
```

`--lang de` builds `index\infosys-1031.db`. The server and `kb_docs.py` read the language of the package the server
uses (English: `infosys-1033.db`).

- `index` reads the newest `BKINFOSYS3*.mshc` of every Help Viewer catalog unless `--mshc` is given. The whole
  package takes minutes (the extractor converts every TwinCAT 3 book; TwinCAT 2 books are left out).
- `search` takes FTS5 syntax: words are ANDed, `"exact phrase"`, `OR`, `NOT`, `prefix*`.
  `--book` is a glob on the book slug, case-insensitive. It filters inside the query, before the row limit.
  When more hits exist, the output ends with `(N of TOTAL hits)`.
- `show` and `decl` take a REF: Infosys URL, `book/pageid`, bare page id, ms-xhelp link or exact title
  (case-insensitive). If several pages share the name, the command prints the candidates and exits with code 1
  (`nothing was chosen`). Pick one and call again with `book/pageid` or `--book`.
- `search` / `show` / `decl` / `books` are the skill's commands (`kb_docs.py docs_search`, `docs_page`,
  `docs_declaration`, `docs_books`) printed as text — the same answers.

### Index logic (`index`) — `twincat_mcp/infosys_build.py`

1. **Locking.** One build at a time: an operating-system lock on `<db>.lock` (`twincat_mcp/kb_lock.py`; POSIX
   `flock`, Windows `msvcrt.locking`), held for the life of the build and released by the operating system however
   it ends. A second build stops with `another build is running (pid …)`; readers know a build runs by finding the
   lock held — no heartbeat, no stale lock to take over. The file holds the progress as JSON (pid, start, phase,
   `pages_done`, `pages_total`); it is never deleted and is empty when no build runs.
2. **Finished build waiting.** If `<db>.building` is complete, of this format and of the current package, it is
   only swapped in (`replaced … with the build finished earlier`) without reading the package again.
3. **Up to date.** The index is `complete` (section 4): the command prints `up to date` and stops. `--force` builds
   anyway.
4. **Build.** An interrupted `<db>.building` of the same format and package is **resumed** (`resuming the
   interrupted build`; pages already in it are skipped); one of another format or package is deleted. The
   pages are converted by the plugin's extractor into `<db>.building` (never into the live file), normalised once
   (rule 4 of `KNOWLEDGE_BASE.md`): plain spaces, no invisible marks, no image placeholders or empty links, table
   cells as plain text, relative links resolved to page addresses while parsing, ms-xhelp links after the pages are
   in (`resolve_help_links`; an id the package does not have stays as it is).
5. **Finish.** Counts, `source_stamp`, `package_file`, `built` and `state = complete` go to `meta`; `fts optimize`;
   rollback-journal mode, so readers open the file read-only with no side files.
6. **Swap.** `<db>.building` replaces the index with `os.replace`. If a reader holds the live file open on Windows,
   the replace is retried for up to **60 s**; if it still fails, the finished file waits as `<db>.building` and is
   swapped in at the next server start or `index`, without reading the package again.

An interrupted or failed build never replaces the live index. The last complete index stays in use.

### Schema

The plugin's Infosys index (`twincat_mcp/infosys.py`): `pages` (url, book, title, kind, Markdown, declaration JSON,
doc version …), `pages_fts` (FTS5 over title, text, code), `catalog`, `code_blocks`, `helpids` (Help Viewer id →
page URL), `books`, `meta`. `meta.indexer_version` is the format (`infosys.INDEXER_VERSION`, 4 in 0.8.11);
`meta.source_stamp` the package's content stamp; `meta.built_by` the builder or plugin version that built it.

## 3. manuals_db.py

### Commands

```
python manuals_db.py [--kb PATH | --db PATH] update  [--dir PATH] [--force]
python manuals_db.py search  QUERY [--doc GLOB] [--limit 10]
python manuals_db.py product CODE [--limit 20]
python manuals_db.py show    DOC PAGE[-PAGE] [--max 4000]
python manuals_db.py toc     DOC [--depth 2]
python manuals_db.py docs    [--filter GLOB]
python manuals_db.py stats   [--json] [--dir PATH]
```

Examples:

```
python manuals_db.py --kb "D:\TwinCAT-MCP\knowledge" update
python manuals_db.py update --kb "D:\TwinCAT-MCP\knowledge" --force
python manuals_db.py product CX9020
python manuals_db.py search "watchdog OR UPS" --doc CX9020 --limit 5
python manuals_db.py toc CX9020 --depth 1
python manuals_db.py show CX9020 52-53 --max 3000
python manuals_db.py stats --json
```

- PDFs are read from `<kb>\manuals\` (recursive, `*.pdf`, case-insensitive) unless `--dir` is given.
- `search --doc` filters inside the query, before the row limit. `(N of TOTAL hits)` when cut.
- `show` and `toc` take DOC as a numeric id, a file name or glob, or a plain word (`*word*`, matched against name and title).
  The match must be exactly one manual.

### Update logic (`update [--dir PATH] [--force]`)

1. **Locking.** One update at a time: the same operating-system lock on `<db>.lock` (`manuals.db.lock`) as the
   Infosys build. A second update stops; `stats` shows the running one.
2. **A copy.** The update works on `<db>.building`, a copy of the index in use (SQLite backup); the index stays in
   use meanwhile. A `.building` left by an interrupted update is discarded first — the copy is taken again from the
   index in use, so the PDFs already in it are not read again.
3. **Removed files.** Documents whose path is no longer on disk are deleted (`removed`).
4. **Each PDF on disk:**
   - known and **size + mtime unchanged** → skipped (`unchanged`); the file is not read.
   - otherwise SHA-256 of the file; known and **same hash** → mtime updated, skipped (`unchanged`).
   - otherwise (new or changed; or everything with `--force`, or everything when the index was written with another
     page text format, see below): old rows dropped, file indexed, committed per file in the copy.
     An exception rolls back that file only and is reported as `FAILED`; the run continues.
5. `fts optimize` when anything changed. `meta.last_update`, `meta.last_result`
   (`N added, N updated, N removed, N unchanged, N failed`) and `meta.text_format` are written.
6. **Swap.** The copy replaces `manuals.db` (`os.replace`, retried for 60 s while a reader has it open on Windows).
   If it still fails, the command says so and nothing is replaced; run `update` again once the readers are closed.
7. **Page text format.** `TEXT_FORMAT` (currently `4`) names the page text and toc rules below. An index whose
   `meta.text_format` differs (a version 1 or 2 database has none: format `1`) is reported as `rebuild_needed` and
   used as it is; the next `update` reads every manual again (`page text format 3 -> 4: every manual is read again`).
   A re-read in which a manual failed (that manual keeps its old rows) leaves the old format in `meta`, so the next
   run reads all manuals again.

### Index logic per PDF

- Backend: **PyMuPDF** (`import pymupdf`, else `import fitz`) if installed; otherwise **pypdf**; if neither, the command
  exits with `pip install pymupdf` (or `pypdf`) for the Python that runs the script. See section 5.
- Page text, with the place of every piece: PyMuPDF `get_text("dict")` (spans with their baseline and size; a page's
  text is its lines in reading order, lines on one baseline side by side, as `get_text("text", sort=True)` gives it);
  pypdf `extract_text(visitor_text=…)` (every text operation with its text matrix; the visited pieces joined give
  exactly `extract_text()`). Cleaning: soft hyphens removed, `ﬁ/ﬂ` expanded, hyphenated line breaks joined
  (`exam-\nple` → `example`), runs of spaces/tabs collapsed, blank lines collapsed.
- What is drawn is the page text (format 4, N37): text of font size 0 and fully transparent text (alpha 0) are left
  out — Beckhoff PDFs carry under every heading an invisible copy of the running header (`| Signal flow diagram` at
  0.0 pt), which doubled headings and toc titles.
- Running headers, footers and margin labels (format 3) are found by their **place**, not by their text — Beckhoff
  prints the current *section* title in the header (`| Signal flow diagram`, a different text in every section, on
  some pages on the same baseline as a heading) and a footer whose page number comes first on some pages
  (`56 Version: 4.8.0 EL3356-00x0`). A piece is running when
  1. it lies in the outer 15 % of the page (top or bottom), and
  2. text of the **same size on the same baseline** (within 2 pt) is there on **at least 40 % of the pages** with
     text (and on 3 pages), and
  3. that baseline is **set apart from the body**: on at least 60 % of those pages the nearest line towards the middle
     of the page is ≥ 1.6 line pitches away (the pitch is the document's median line distance). Evaluated from the
     body outwards: a header line that passed is transparent for a second header line further out; one that failed
     (the body's first line) is not.
  The running pieces are left out of the page text and of the font-heading toc. What stays: the body's first line and a
  table header row repeated at the top of continuation pages (`Index (hex) Name Meaning Data type Flags Default` — the
  next row follows at one pitch), a heading that shares the header's baseline (another size). The update prints the
  number of removed pieces per manual.
- Fallback, only when the backend gives no positions (an old pypdf): the text-repetition rule of format 2 — the first
  and last three lines of every page, text repeated (digits as `#`) on 3 pages and 5 % of the pages, or on 2 when it
  is a chapter title; numbers that change must behave like a page number: **one** of them (first, last or any) grows
  with the page, the others stay (`56 Version: 4.8.0 …` and `… Version: 4.8.0 57`; `Table 3 of 9` is kept); signal
  words (`NOTE`, `NOTICE`, `WARNING` …), numbered toc headings and wrapped heading lines are kept; a removed header glued
  to a heading is cut off. This rule cannot tell a table header row from a running header — the reason format 3 uses
  positions.
- Title: PDF metadata title; if empty, the largest text span on page 1.
- Chapters (`toc`):
  1. the PDF **bookmarks** if there are any;
  2. otherwise **from the fonts** (backend `pymupdf+font-headings`): body size = the font size with the most characters;
     heading candidates = **bold** lines with size ≥ **1.5 × body size**; a line holding only a number (`4`, `4.2`) is joined
     with the heading that follows; a number glued to the title in one span (`10Error handling`) is split; lines
     longer than 120 chars are ignored; level = depth of the number; unnumbered headings → level 1 when the manual
     uses numbers, else the font-size rank. A heading line directly below a heading line of the same size, on the same
     page and without a number of its own, continues that heading (a heading wrapped over two lines, format 2). The
     lines are read without the running pieces (format 3): a header on a heading's baseline was glued into the title
     (`1.1 Product overview| Product overview`).
  3. both sources (format 2): in a manual that numbers its chapters, an unnumbered entry on the same page as the entry
     before it is that entry's second line when it starts in lower case or with `)`, `]`, `}`, or when the entry before
     ends with `-`, `,`, `(`, `/`, `&` or a joining word (`and`, `or`, `of`, `the`, `for`, `with`, `to`, `in`, `on`,
     `und`, `oder`, `der`, `die`, `das`, `des`, `für`, `mit`, `zur`, `zum`, `von`). A trailing hyphen before a lower-case
     continuation is dropped (`Brücken-` + `kapazität`).
- Section per page: the label is the **last two levels** active at the end of the page (`10 Error handling > 10.1 Diagnostic LEDs`).
  When two headings start on one page, the page carries the later one.
- Product codes: regex over each page's text, counted per page:
  `\b((EL|ELM|ELX|ES|EK|EP|EPP|EPI|ER|EQ|EM|EJ|EZ|EX|KL|KM|KS|BK|BC|BX|CX|CP|CU|C6|C7|AX|AMP|AM|AG|AT|FC|IP|IL|IE|LC|TF|TE|TC|ZB|ZK|ZS)\d{3,5}(-\d{4})?)\b`
- Pages with no text are stored. A PDF with no text at all is flagged `no text layer (scanned?)`. OCR is not included.

### Schema (version 5; tables unchanged since version 2)

```sql
doc(id PRIMARY KEY, path UNIQUE, name, size, mtime, sha256, title, pages, text_pages, indexed, backend)
  -- path is stored absolute (see section 6)
page(doc, page, section, text, PRIMARY KEY(doc, page))
toc(doc, level, title, page)
product(code, doc, page, n)                          -- INDEX on code
fts  USING fts5(text, section, doc UNINDEXED, page UNINDEXED, tokenize='unicode61 remove_diacritics 2')
meta(key PRIMARY KEY, value)
  -- state (updating | idle), pid, started, folder, builder ("manuals_db.py 5"), files_total, files_done, heartbeat,
  -- last_update, last_result, text_format ("4")
```

A version-1 database has no `meta` table; the next `update` creates it.

### Query logic

| Command | Logic | Output |
|---|---|---|
| `search QUERY [--doc GLOB] [--limit 10]` | FTS5 `MATCH`, ranked by `bm25(fts, 1.0, 4.0)` (section heading weighs 4× text). `--doc` resolves to document ids first and adds `doc IN (…)` to the query, so the limit applies after the filter | `file \| p<page> \| section \| snippet` (16 tokens, hits in `[ ]`) |
| `product CODE [--limit 20]` | exact code or `CODE-xxxx` variants, grouped per manual, sorted by mentions | `file \| N mentions \| pages 1,3,4 …` (first 15 pages) |
| `show DOC PAGE[-PAGE] [--max 4000]` | one manual (section 3 matching), page range, cut at `--max` | header (`file \| title \| pages \| path`) + `--- p<n> \| section` blocks |
| `toc DOC [--depth 2]` | chapters up to the depth, indented by level | `title  p<page>` |
| `docs [--filter GLOB]` | overview | id, file, pages, text pages, title |
| `stats [--json]` | state from `manuals_state` (section 4) | counts, DB size, the state message; `--json` gives the full object |

## 3b. esi_db.py

### Commands

```
python esi_db.py [--kb PATH | --db PATH] update [--force] [--esi-dir DIR ... | --no-esi-dirs] [--no-twincat | --twincat]
python esi_db.py stats   [--json]
python esi_db.py lookup  QUERY [--revision REV] [--detail newest|summary|full] [--limit 5]
python esi_db.py search  QUERY [--kinds device,object,diag,module] [--limit 20]
python esi_db.py object  DEVICE INDEX [--revision REV]
python esi_db.py diag    CODE [--device DEVICE]
python esi_db.py module  IDENT [--device DEVICE]
```

The server runs `update` itself (the first build at its start, later `kb_rebuild(index='esi')` once approved) with its
`knowledge.esi_dirs` as `--esi-dir` (or `--no-esi-dirs`); without either, `update` uses the folders the index in use
was built with. `--no-twincat` leaves TwinCAT's own ESI folders out and is recorded in the index (its state and later
updates keep it); `--twincat` reads them again. A work file (`esi.db.building`) left next to a complete, current index
is discarded by the next `update`. Standard library only; the queries print the JSON of the server's `hw_*` tools.

### What is read (`twincat_mcp/esi_index.py`, format 2)

Every `*.xml` below the sources whose root element is an ESI document — files of other roots are listed (`not_esi`)
and skipped:

| root | what is stored |
|---|---|
| `EtherCATInfo` | each `Device`: type, product code, revision, names (English, German), group, vendor, `FileVersion`, physics, E-bus current, URLs, the picture `RES\<type>_standard.*` beside the file, mailbox protocols, sync managers, FMMUs, DC operation modes, `HideType`, every PDO with every entry (a gap — index 0 — is kept with `gap`, it occupies bits), excludes, `Virtual`, slot-dependent indices, the predefined PDO assignments (`AlternativeSmMapping`: PDOs per SM, DC mode, init commands), slots, the CoE dictionary, inline diagnosis messages, `DictionaryFile` / `DiagFile`; each `Descriptions\Modules\Module` |
| `EtherCATModule` | each module: ModuleIdent, type, name, PDOs, dictionary, diagnosis messages |
| `EtherCATDict` | a dictionary named by a device's `DictionaryFile` (AX5000) |
| `EtherCATDiag` | a diagnosis catalogue named by a device's `DiagFile`: text id, message, description, causes and remedies, class, reaction / reset / type as their texts |

- **PDO templates** (`<TxPdo Ref="…" Chn="n"/>` and `Descriptions\Pdos\Pdo[@RefName]`, Beckhoff's compact files):
  resolved per channel — `ChnFac` adds `n × ChnFac` to an index or sub-index, a name's `#1` becomes `1 + n × ChnFac`.
  A template the file does not hold is kept as `unresolved` with the reason. Such a file is read twice (templates
  first); every other file once, element by element, each device dropped once read (memory stays bounded — the
  88 MB `ELM310x.xml` included). A dictionary or diagnosis file of its own is read whole (the AX5000 dictionaries:
  1.6 MB each).
- **Several profiles** of one device (the schema allows one per channel; Beckhoff's files have one): their
  dictionaries, diagnosis messages and the files they name are joined; an object or message two of them give
  differently is listed in `dictionary_conflicts` / `diag_conflicts` (the first one's is kept).
- **A description replaced** by a newer one (another folder, higher FileVersion) leaves nothing behind: objects and
  messages only it used are dropped before the index is complete, so the counts and `hw_search` are of what is kept.
- **Dictionary**: objects with type, bit size, access, PDO mapping, backup / setting flags, comment; sub-indices from
  the object's data type (bit offset, type, access); an array type's elements one by one (`SubIndex 001` …, base
  type, own bit offset); `Info\SubItem` defaults matched by name; `DefaultData` decoded little-endian for elementary
  types (kept as `default_raw`), an enumeration's value with its text; min / max; the unit code as written.
  Indices in hex (`#x8000`) or decimal (`0410` in AX5000 Dict files).
- **Stored once by content**: an object, a dictionary, a diagnosis message, a catalogue — most revisions share them.
- **Duplicates**: a file whose content another source already gave is read once (its devices list it in `also_in`).
  A revision (type, product code, revision) in several files: the one with the higher vendor `FileVersion`, then the
  newer file, is kept; the others are named in `also_in` (identical) or `differs_in` (different).
- **Modules are per file**: the same ModuleIdent is a different module in each AX8000 module file. A device's slot
  module is resolved when read, by rule: the device's own file; else the module file paired with it by name
  (`<prefix> Modules….xml`, `<prefix>` beginning the device file's name, the longest first); else the only module
  with that ident. Several different modules at the first rule that applies: all are named, none chosen. The same
  rule gives each module the devices whose slots take it: their type, name and group are part of the module's
  search text, so `hw_search … kinds=module` finds a module by its device (format 2, 0.8.14 retest N156 — the
  AX8000 modules are named DeviceMain, SynchronousMotor …).
- A `DictionaryFile` / `DiagFile` is looked up beside the device file as written, else as the one source file of that
  name; not found: the device says so (`dictionary_note`), `stats` counts it (`missing_refs`).

### Build

One build at a time (`<db>.lock`); the build writes `<db>.building` and replaces the index when complete (60 s retry
while a reader has it open; otherwise it waits as `waiting_to_replace` and is swapped in by the next `update`); an
interrupted build replaces nothing and the next one starts over. Without `--force`, an index of this format built from
exactly these files (paths, sizes, times — `sources_stamp`) is left as it is (`up to date`). Full-text search
(`search`) needs SQLite with FTS5; without it everything else works.

### Schema (format 2; the same tables as format 1 — format 2 adds the host devices to a module's search text)

```sql
files(id, path, source, kind, size, mtime, hash, same_as, items, error)
obj(id, hash UNIQUE, idx, name, type, access, pdo_mapping, data)          -- data: zlib JSON of the object
dicts(id, hash UNIQUE, objects, origin)            dict_obj(dict_id, idx, obj_id)
msg(id, hash UNIQUE, text_id, message, data)       diag_sets(id, hash UNIQUE, messages, origin)
diag_msg(set_id, text_id, msg_id)
devices(id, type, name, product_code, revision, vendor, group_type, file_version, file_id, dict_id, diag_set,
        pdo_count, pdo_entries, objects, also_in, differs_in, data)       -- data: zlib JSON of the device
modules(id, ident, type, name, vendor, file_id, dict_id, diag_set, data)
search USING fts5(kind, ref, title, body)          -- devices, objects, diagnosis messages, modules
meta(k, v)  -- format, state, sources, extra_dirs, sources_stamp, counts, errors, missing_refs, not_esi, built, built_by
```

## 4. Index state

The scripts report the real state with `stats` (and `--json`). The same code gives it to `kb_docs.py status` (and
`source.state` in every answer), to the server at every connection (instructions line `Offline documentation, state
at this connection (<kb>): …`), to `tc_doctor` (`offline_docs`) and to `kb_status` → `docs`.

| state | meaning | what happens |
|---|---|---|
| `complete` | built by this format from the source that is here | nothing |
| `rebuild_needed` | built by another format (a plugin update changed what is stored), or never finished | used as it is until the rebuild is approved |
| `outdated` | the source changed: another Help Viewer package; PDFs added, changed or removed | used as it is until the rebuild is approved |
| `missing` | no index file | the server's first build starts by itself (manuals: when `manuals\` holds PDFs and the server's Python reads PDFs); or `index` / `update` |

A rebuild runs only when you run the command (`index`, `update`) or approve `kb_rebuild(index='infosys'|'manuals')`.

Build in progress (shown in addition to the state; `rebuild` for Infosys, `updating` for manuals):

| state | meaning | what to do |
|---|---|---|
| `running` | a build holds the lock | wait; the live index stays in use |
| `interrupted` | `<db>.building` exists and no build holds the lock | the next `index` resumes it; the next `update` starts again from the index in use |
| `waiting_to_replace` | Infosys: the finished build could not replace the live file (a reader had it open) | swapped in at the next server start or `index` |
| `stale` | Infosys: a `.building` of another format or package, or one left next to a complete, current index | nothing — the next build discards it (`index --force` resumes a same-package one) |

Infosys `stats` also shows the format, the package and the counts (`links`, `links_resolved`, `built_by`). Manuals
`stats --json` lists:

| field | meaning |
|---|---|
| `not_indexed` | PDF in `manuals\`, not in the index (new) |
| `changed_since_index` | size or mtime differs from the index |
| `indexed_but_gone` | in the index, file no longer on disk |
| `no_text_layer` | indexed, but no page has text (scanned?) |
| `text_format`, `text_format_expected` | the index's page text format and the builder's |

## 5. PyMuPDF and pypdf

- The manuals script works with **PyMuPDF** (`pip install pymupdf`) or **pypdf** (`pip install pypdf`), installed into
  the Python that runs the script. It uses PyMuPDF when it can import it, else pypdf.
- PyMuPDF is licensed under AGPL-3.0. It is **not** part of the twincat plugin. Install it yourself if you accept its license.
- Text quality differs: PyMuPDF with `sort=True` keeps reading order on two-column pages and finds headings from
  fonts; pypdf does not. The backend used per manual is stored in `doc.backend`.

## 6. Stubs in `<kb>\tools\` (nothing to update after a plugin update)

`<kb>\tools\infosys_local.py` and `<kb>\tools\manuals_db.py` are **stubs** (`stub.py` in this folder, saved under the
builder's name). A stub holds no builder code: it finds the builders of the installed plugin and runs the one with its
own file name, with the same arguments and the same Python, so `tools\manuals_db.py update` always runs the current
`manuals_db.py`. A stub never changes; a plugin update needs no copying.

Where a stub finds the builders, first match wins:

1. `TCMCP_BUILDERS` — a folder holding `infosys_local.py` / `manuals_db.py`;
2. `<TCMCP_HOME or ~\.twincat_mcp>\plugin_location.json` — written by the twincat MCP server at every start
   (`builders`: this folder of the running plugin, `version`);
3. the newest `twincat-mcp` version in the Claude Code plugin cache (`~\.claude\plugins\cache\*\twincat-mcp\<version>`).

None found: one line saying what was tried, exit code 1. `TCMCP_STUB_VERBOSE=1` prints which builder runs. The stub
sets `TCMCP_STUB_DIR` to its own folder, so the builder's "folder above the script" (section 1, step 4) is the folder
above the stub — `<kb>`.

**Installed by the server, once.** At every start (and on `kb_set_location`) the server first records where its
builders are (`plugin_location.json`), then replaces a file in `<kb>\tools\` with the stub **only when it is a file
this plugin shipped**: its content (sha256, CRLF read as LF) is one of the builder or stub versions that ever left the
repository (`SHIPPED_BUILDERS` / `SHIPPED_STUBS` in `twincat_mcp/knowledge.py`), or a copy of the builder the
installed plugin ships. The replaced file is kept beside it as `<name>.v<version>.bak`. Anything else — your own
script under that name, an edited copy, a newer one — is left alone and reported with the command that switches it
to the stub; a missing file is not created. `kb_status` → `docs.builders` shows `copy_kind` (`stub`, `copy` = a
shipped file the next start replaces, `other` = left alone), `docs.builder_upkeep` what the last start did; the
commands it suggests use the stub in `<kb>\tools\`, else the plugin's own builder (never a file it left alone).

The builders run from the plugin folder they ship in (version 3 / 5 use the plugin's `twincat_mcp` package and
`kb_docs.py` beside them); a copy on its own in `tools\` says so and stops. On a PC without the plugin, copy the whole
plugin folder and run the builders from its `skills\twincat-docs\builders\`.

Stored PDF paths are absolute. Moving the knowledge folder makes the next `update` index every manual again
(accepted limitation).

## 7. Maintenance

| when | do |
|---|---|
| new or changed PDF | copy into `<kb>\manuals\…` → `tools\manuals_db.py update` (or approve `kb_rebuild(index='manuals')`) |
| Help Viewer package updated (TwinCAT / XAE update) | `tools\infosys_local.py index` (or approve `kb_rebuild(index='infosys')`) |
| a state `rebuild_needed` / `outdated` | the same commands; until then the index is used as it is |
| a build is `interrupted` | `index` resumes it; `update` starts again from the index in use |
| `waiting_to_replace` | close the programs that read the index, then `index` (or restart the server) |
| a PDF gives bad chapters | `toc DOC`; bookmarks win over font detection |
| rebuild from scratch | `index --force` / `update --force` |
| move the knowledge folder | `--kb`, `TCMCP_KNOWLEDGE_DIR` or `kb_set_location` to the new folder; manuals re-index (section 6) |

## 8. Limits

- Section label is per page (last heading on the page); headings sharing a page show the later one.
- Font-heading detection assumes bold headings ≥ 1.5 × body size (true for Beckhoff manuals); other layouts need
  the factor changed in `_toc_from_fonts`.
- A heading whose title starts with a number followed by a capital (`24V …`) can be read as a chapter number `24`.
- Scanned PDFs have no text layer; OCR is not included.
- Infosys: images are dropped; tables are stored as pipe rows with plain-text cells (merged cells are not
  reconstructed); a cell's link is kept as the page of its type (`type_ref`), page links in the Markdown.
- Running headers are found by position (format 3): a header that is not set apart from the body (no larger gap
  to the first body line than between body lines) or that is on fewer than 40 % of the pages stays in the text.
- The builders run on Python 3.12 and newer only. The test suite runs on 3.12 (Windows and Linux); the refusal message is tested by running each script with `sys.version_info` set to 3.11.
