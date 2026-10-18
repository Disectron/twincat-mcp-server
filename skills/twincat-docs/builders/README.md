# Builders: infosys_local.py and manuals_db.py

Two standalone scripts that build the offline documentation databases read by the `twincat-docs` skill
(`kb_docs.py`) and by the twincat MCP server. Version 2 (plugin 0.8.9). Standard library only, except that
PDF text needs PyMuPDF or pypdf (see section 5).

| script | reads | writes |
|---|---|---|
| `infosys_local.py` | Beckhoff Infosys package `BKINFOSYS3*.mshc` (Microsoft Help Viewer) | `<kb>\helpviewer\infosys_en.db` |
| `manuals_db.py` | PDF files under `<kb>\manuals\` (sub-folders allowed) | `<kb>\manualsdb\manuals.db` |

## 1. Requirements and location

- **Python 3.12 or newer.** Both scripts check the version first and refuse older versions with a message:
  `infosys_local.py needs Python 3.12 or newer; this is 3.9.x ...`. The files still compile on older Pythons so
  that the message is shown. Use the python.org build, for example
  `C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`.
- **Knowledge folder `<kb>`** is found in this order:
  1. `--kb PATH` on the command line,
  2. the environment variable `TCMCP_KNOWLEDGE_DIR`,
  3. the folder chosen with the plugin's `kb_set_location` (pointer file `~\.twincat_mcp\knowledge_location.json`,
     or `%TCMCP_HOME%\knowledge_location.json` when `TCMCP_HOME` is set),
  4. the folder above the script, when that folder is a knowledge folder (it contains `helpviewer\` or `knowledge.db`
     for `infosys_local.py`; `manualsdb\` or `knowledge.db` for `manuals_db.py`). This is why a copy in `<kb>\tools\` works,
  5. `D:\TwinCAT-MCP\knowledge` (the default, `FALLBACK_KB` in each script).
- `--kb` and `--db` may be given before or after the command: `--kb D:\kb index` and `index --kb D:\kb` are the same.
- In a new folder, neither `helpviewer\` nor `knowledge.db` exists yet, so the folder-above lookup does not apply.
  Pass `--kb` on the first run.
- Each script has its own `--db` override: the database file itself.

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

`--lang de` builds `helpviewer\infosys_de.db`. The plugin's `kb_docs.py` reads `infosys_en.db` only.

- `index` reads the newest `BKINFOSYS3*.mshc` in `ContentStore\EN-US` (DE-DE for `--lang de`) unless `--mshc` is given.
  Takes about 150 s for the full package.
- `search` takes FTS5 syntax: words are ANDed, `"exact phrase"`, `OR`, `NOT`, `prefix*`.
  `--book` is a glob on the book slug, case-insensitive. It filters inside the query, before the row limit.
  When more hits exist, the output ends with `(N of TOTAL hits)`.
- `show` and `decl` take a REF: Infosys URL, `book/pageid`, bare page id, or exact title (case-insensitive).
  If several pages share the name, the command prints the candidates (`book/pageid`, title, version) and exits
  with code 1. Nothing is shown. Pick one and call again with `book/pageid` or `--book`.
- `decl` prints the intro, every code block, the Requirements / Prerequisites / Voraussetzungen section,
  `Also in:` (other books with a page of the same title) and `Links:`.
- `show` prints the links of the part shown, as `text → book/pageid`.

### Index logic (`index`)

1. **Locking.** One build at a time, through `<db>.lock` (`infosys_en.db.lock`). The lock file holds pid and start time.
   Its modification time is the heartbeat, refreshed every 15 s by the running build (also during long single steps
   such as `fts optimize`). A lock whose heartbeat is older than **120 s** belongs to a dead build and is taken over. A live lock makes the command stop with
   `another index build is running (pid …)`.
2. **Finished rebuild waiting.** If `<db>.building` is complete for the current package, the command only swaps it in
   (`replaced … with the rebuild finished earlier`) without reading the package again.
3. **Up to date.** If `meta.source` equals the package stamp, `schema` is 2 and `state` is `complete`, the command
   prints `up to date` and stops. `--force` rebuilds anyway.
4. **Build.** A leftover `<db>.building` is deleted. The new index is written to `<db>.building` (never to the live
   file). For every `.html`/`.htm` in the package:
   - `book` = first path part, `pid` = file stem.
   - metadata from the first 6,000 chars (`<meta name=… content=…>`, first occurrence wins), breadcrumb from the first 12,000 chars.
   - body text: from `<article` (else `<body`), converted to compact text. `h1–h4` → `#`…`####`; `p/div/li/br/dt/dd` → new line
     (`li` → `- `); `<pre>` → fenced block; tables → `| cell | cell |` rows (`|` in a cell → `/`);
     `script/style/header/nav/footer/head` skipped; images dropped; links keep their **text** in the body.
   - every `<a href>` goes to the `link` table (see schema). ms-xhelp ids are resolved through `page.help_id`;
     relative and Infosys-URL links are resolved to `book/pageid`. Links to pages not in the package, and external links,
     keep only `href`.
   - progress (`pages_done`, `pages_total`, `heartbeat`) is written to `<db>.building` every 2,000 pages.
5. **Finish.** `UPDATE` resolves the links, the counts are written to `meta`, `fts optimize` runs, `state` becomes `complete`.
6. **Swap.** `<db>.building` replaces `infosys_en.db` with `os.replace`. If a reader (an MCP tool, `kb_docs.py`) holds the
   live file open on Windows, the replace is retried for up to **60 s**. If it still fails, the finished file waits in
   `<db>.building` and the command says so. The next `index` swaps it in without re-reading the package.

An interrupted build (also one run with `--force`) never replaces the live index. The last complete index stays in use.

### Schema (version 2)

```sql
meta(key PRIMARY KEY, value)
  -- schema (2), source (package stamp "<file>|<size>|<mtime>"), lcid (1033|1031), indexed, state (building|complete),
  -- pages, books, links, links_resolved, mshc_path, builder ("infosys_local.py 2"), python, started, heartbeat,
  -- pages_done, pages_total, pid
page(pid, book, title, help_id, toc_parent, version, crumb, body, PRIMARY KEY(book, pid))
  -- index page_title (title), page_help (help_id)
fts  USING fts5(title, body, book UNINDEXED, pid UNINDEXED, tokenize='unicode61 remove_diacritics 2')
link(book, pid, n, text, href, hid, target_book, target_pid)
  -- index link_page (book, pid); n = position of the link on the page; hid = ms-xhelp id;
  -- target_book / target_pid NULL when the link is external or points to a page not in the package
```

A version-1 index has no `link` table and no `schema` meta key. Its body text is the same, so it still answers searches.

### Query logic

| Command | Logic | Output |
|---|---|---|
| `search QUERY [--book GLOB] [--limit 10]` | FTS5 `MATCH`, ranked by `bm25(fts, 8.0, 1.0)` (title weighs 8× body). `--book` is added to the `WHERE` clause (`fnm(book, GLOB)`), so the limit applies after the filter. The total is counted with the same filter | one line per hit: `title \| book/pid \| snippet` (snippet: body, 18 tokens, hits in `[ ]`); `(N of TOTAL hits)` when cut |
| `show REF [--section NAME] [--max 4000] [--book GLOB]` | REF resolution as described under Commands. `--section`: keep only the heading blocks whose heading contains NAME. Cut at `--max` chars with the remaining count. Ambiguous REF → candidates, exit 1 | header (`title \| book/pid \| version` + web URL), the text, `Links:` for the part shown |
| `decl SYMBOL [--book GLOB]` | same resolution; prints the intro (≤ 400 chars, text before the first code block), every fenced code block, the Requirements section, `Also in:`, `Links:` | the cheapest answer for inputs/outputs/struct members and the TwinCAT / library version |
| `books [--filter GLOB]` | `GROUP BY book` | `book  pages` |
| `stats [--json]` | state from `index_state` (section 4) | one line; with `--json` the full state object |

QUERY is FTS5 syntax: words are ANDed, `"exact phrase"`, `OR`, `NOT`, `prefix*`.

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

1. **Locking.** One update at a time through `<db>.lock` (`manuals.db.lock`). Heartbeat: the lock's modification time
   (refreshed every 15 s by the running update) and `meta.heartbeat` (before each file). A lock silent for **300 s** is
   taken over. A live lock makes the command stop.
2. **Removed files.** Documents in the database whose path is no longer on disk are deleted (`removed`).
3. **Each PDF on disk:**
   - known and **size + mtime unchanged** → skipped (`unchanged`); the file is not read.
   - otherwise SHA-256 of the file; known and **same hash** → mtime updated, skipped (`unchanged`).
   - otherwise (new or changed; or everything with `--force`): old rows dropped, file indexed, committed **per file**.
     An exception rolls back that file only and is reported as `FAILED`; the run continues.
   - progress is kept: a file committed before an interruption is `unchanged` on the next run.
4. `fts optimize` when anything changed. `meta.last_update` and `meta.last_result` are written
   (`N added, N updated, N removed, N unchanged, N failed`).

### Index logic per PDF

- Backend: **PyMuPDF** (`import pymupdf`, else `import fitz`) if installed; otherwise **pypdf**; if neither, the command
  exits with `pip install pymupdf` (or `pypdf`) for the Python that runs the script. See section 5.
- Page text: PyMuPDF `get_text("text", sort=True)` (reading order, also for two columns); pypdf `extract_text()`.
  Cleaning: soft hyphens removed, `ﬁ/ﬂ` expanded, hyphenated line breaks joined (`exam-\nple` → `example`),
  runs of spaces/tabs collapsed, blank lines collapsed.
- Title: PDF metadata title; if empty, the largest text span on page 1.
- Chapters (`toc`):
  1. the PDF **bookmarks** if there are any;
  2. otherwise **from the fonts** (backend `pymupdf+font-headings`): body size = the font size with the most characters;
     heading candidates = **bold** lines with size ≥ **1.5 × body size**; a line holding only a number (`4`, `4.2`) is joined
     with the heading that follows; a number glued to the title in one span (`10Error handling`) is split; lines
     longer than 120 chars are ignored; level = depth of the number; unnumbered headings → level 1 when the manual
     uses numbers, else the font-size rank.
- Section per page: the label is the **last two levels** active at the end of the page (`10 Error handling > 10.1 Diagnostic LEDs`).
  When two headings start on one page, the page carries the later one.
- Product codes: regex over each page's text, counted per page:
  `\b((EL|ELM|ELX|ES|EK|EP|EPP|EPI|ER|EQ|EM|EJ|EZ|EX|KL|KM|KS|BK|BC|BX|CX|CP|CU|C6|C7|AX|AMP|AM|AG|AT|FC|IP|IL|IE|LC|TF|TE|TC|ZB|ZK|ZS)\d{3,5}(-\d{4})?)\b`
- Pages with no text are stored. A PDF with no text at all is flagged `no text layer (scanned?)`. OCR is not included.

### Schema (version 2)

```sql
doc(id PRIMARY KEY, path UNIQUE, name, size, mtime, sha256, title, pages, text_pages, indexed, backend)
  -- path is stored absolute (see section 6)
page(doc, page, section, text, PRIMARY KEY(doc, page))
toc(doc, level, title, page)
product(code, doc, page, n)                          -- INDEX on code
fts  USING fts5(text, section, doc UNINDEXED, page UNINDEXED, tokenize='unicode61 remove_diacritics 2')
meta(key PRIMARY KEY, value)
  -- state (updating | idle), pid, started, folder, builder ("manuals_db.py 2"), files_total, files_done, heartbeat,
  -- last_update, last_result
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

## 4. Index state

Both scripts report the real state with `stats` (and `--json`). The same rules are used by `kb_docs.py status`,
by the server at every connection (instructions line `Offline documentation, state at this connection (<kb>): …`),
by `tc_doctor` (`offline_docs`) and by `kb_status` → `docs`.

### infosys_en.db (`infosys_local.py stats`)

| state | meaning | what to do |
|---|---|---|
| `complete` | built from the current package, schema 2 | nothing |
| `outdated` | the package file changed since the build, or a newer `BKINFOSYS3*.mshc` exists in its folder. The old index stays in use | `index` |
| `old_format` | complete text, version-1 build: no `link` table, so no links | `index` once (upgrades it) |
| `incomplete` | a version-1 build that stopped half-way | `index` |
| `missing` | no index file | `index` |

Rebuild in progress (shown in addition to the state):

| rebuild state | meaning | what to do |
|---|---|---|
| `running` | a build is working; heartbeat younger than 120 s | wait; the live index stays in use |
| `interrupted` | `<db>.building` exists, no heartbeat for 120 s | `index` builds again (the leftover is deleted) |
| `waiting_to_replace` | the finished rebuild could not replace the live file (a reader had it open) | `index` swaps it in |

The `stats` output also shows the package path and the counts (`links`, `links_resolved`, `builder`).

### manuals.db (`manuals_db.py stats --json`)

`state` is `up_to_date` or `pending`. `pending` lists the PDFs that are not reflected in the database:

| field | meaning |
|---|---|
| `not_indexed` | PDF in `manuals\`, not in the database (new) |
| `changed_since_index` | size or mtime differs from the database |
| `indexed_but_gone` | in the database, file no longer on disk |
| `no_text_layer` | indexed, but no page has text (scanned?) |
| `updating` | `state` `running` or `interrupted`, with `files_done` of `files_total`. Interrupted: run `update` again |

Pending items are fixed by `update`.

## 5. PyMuPDF and pypdf

- The manuals script works with **PyMuPDF** (`pip install pymupdf`) or **pypdf** (`pip install pypdf`), installed into
  the Python that runs the script. It uses PyMuPDF when it can import it, else pypdf.
- PyMuPDF is licensed under AGPL-3.0. It is **not** part of the twincat plugin. Install it yourself if you accept its license.
- Text quality differs: PyMuPDF with `sort=True` keeps reading order on two-column pages and finds headings from
  fonts; pypdf does not. The backend used per manual is stored in `doc.backend`.

## 6. Copies in `<kb>\tools\`

The plugin ships both scripts in `skills\twincat-docs\builders\`. The copies in `<kb>\tools\` are optional: they make
the commands short. Replace any older copy with the version 2 files. Copy **both** files:

```
copy "<plugin>\skills\twincat-docs\builders\infosys_local.py" "<kb>\tools\"
copy "<plugin>\skills\twincat-docs\builders\manuals_db.py"   "<kb>\tools\"
```

They find `<kb>` as the folder above `tools\` (section 1, step 4), so no path needs to be edited. The server's
`kb_status` → `docs.builders` reports `shipped_version` and `copy_in_tools` / `copy_version`, and notes when the copy in
`<kb>\tools` is older than the one shipped with the plugin.

Stored PDF paths are absolute. Moving the knowledge folder makes the next `update` index every manual again
(accepted limitation).

## 7. Maintenance

| when | do |
|---|---|
| new or changed PDF | copy into `<kb>\manuals\…` → `manuals_db.py update --kb <kb>` |
| Help Viewer package updated (TwinCAT / XAE update) | `infosys_local.py index --kb <kb>` (no-op when unchanged) |
| `stats` says `outdated`, `old_format`, `incomplete`, `missing` | `index` |
| `stats` says a rebuild is `interrupted` | `index` |
| `stats` says `waiting_to_replace` | `index` (close programs that read the database first, if it says it is still open) |
| an update is `interrupted` | `manuals_db.py update` |
| a PDF gives bad chapters | `toc DOC`; bookmarks win over font detection |
| rebuild from scratch | delete the `.db` file (and any `.building` / `.lock` beside it), then `index` / `update --force` |
| move the knowledge folder | `--kb`, `TCMCP_KNOWLEDGE_DIR` or `kb_set_location` to the new folder; manuals re-index (section 6) |

## 8. Limits

- Section label is per page (last heading on the page); headings sharing a page show the later one.
- Font-heading detection assumes bold headings ≥ 1.5 × body size (true for Beckhoff manuals); other layouts need
  the factor changed in `_toc_from_fonts`.
- A heading whose title starts with a number followed by a capital (`24V …`) can be read as a chapter number `24`.
- Scanned PDFs have no text layer; OCR is not included.
- Infosys: images are dropped; tables are flattened to pipe rows (merged cells are not reconstructed). Links are kept
  in the `link` table; the body text shows only their text.
- A version-1 index answers searches but has no links. `docs_page` on it says so (`links_note`).
- The builders run on Python 3.12 and newer only. The test suite runs the builder tests on 3.12; older versions are checked for the refusal message only.
