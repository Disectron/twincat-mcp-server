---
name: twincat-docs
description: Use for ANY Beckhoff documentation question — what a library FB/function/struct/enum does, its inputs/outputs/types (ADSREAD, MC_MoveAbsolute, FB_FileOpen …), error codes, TwinCAT concepts, code examples, hardware details from manuals (LEDs, wiring, diagnostics of an IPC/terminal), or EtherCAT device data from the ESI files (PDOs, CoE objects with defaults, diagnosis text ids). Looks it up OFFLINE in the local Infosys (Help Viewer package), PDF-manual and ESI knowledge base. Never use ads_* tools for documentation.
---

# twincat-docs — offline Beckhoff documentation

## Not the ADS tools
`ads_read`, `ads_symbol_info`, `ads_list_symbols`, `ads_types`, `ads_call_method` read the **running PLC's** memory
and symbol table. They never return documentation. Names in Structured Text examples are not tools either:
`fbRead : ADSREAD := (…);` … `fbRead(READ := TRUE)` declares and calls an **instance** of the library function
block `ADSREAD`. Look up `ADSREAD` here.

## How to call
`kb_docs.py` sits in this skill's folder (the base directory shown when the skill loads). Run it with Python
3.12 (standard library only; `python`, else `py -3.12`, else the Python the twincat plugin was installed with — an older
Python gets one line saying so, not an answer):

```
python "<skill folder>/kb_docs.py" <command> key=value … [--kb PATH]
python "<skill folder>/kb_docs.py" docs_search "query=fbRead( READ := FALSE )" limit=3
```

- Quote a value that has spaces **together with its key**: `"query=a b"`.
- Output: one JSON object. Exit 0 = answer. Exit 1 = `{"error": code, "message", …}` with codes `ambiguous`,
  `not_found`, `bad_param`, `bad_query`, `empty_query`, `no_index`, `unknown_command`, `internal`.
- `help` lists the commands and parameters. `status` shows where the data is and what is indexed.

## Which command
| Question | Command |
|---|---|
| Inputs/outputs/members of a known FB, function, struct or enum | `docs_declaration symbol=ADSREAD book=tcplclib_tc2_system` |
| Concept, how-to, error text, error code, anything else in Infosys | `docs_search query=… [book=glob] [limit=1-50]` → `ref` → `docs_page ref=… section=…` (the plugin's infosys_search: identifiers count as their words, titles that name the query first, an error code's defining table first — `code_note` —, one page in several books once — `same_page_in`) |
| A page or a link from an answer | `docs_page ref=<book/pageid, Infosys URL, page id, exact title, ms-xhelp link> [section=] [offset=] [max_chars=200-20000]` |
| Which books exist | `docs_books filter=tcplclib_tc2_*` |
| Hardware: which manual covers a product | `manual_product code=EL3102` (also matches EL3102-xxxx; a variant no manual names, `CX9020-0130`, falls back to the base product — `matched_as` says so) |
| Hardware: LEDs, wiring, diagnostics, error codes | `manual_search query=… [doc=CX9020]` → `manual_page doc=… pages=N-M` (≤ 21 pages) |
| Chapters of a manual | `manual_toc doc=CX9020 [depth=1-6]` |
| Which manuals are indexed | `manual_docs [filter=…]` |
| EtherCAT device from its ESI: PDOs and entries, PDO assignments, DC modes, slots, CoE object list, the indexed manuals that name it | `hw_lookup query=EL3356-0010 [revision=#x…] [detail=newest\|summary\|full] [limit=1-30]` (a product code `#x0d1c3052` / `0x0d1c3052` / decimal `219951186`, or part of a name works too; a revision that is not indexed: `note` + `revisions_available`) |
| One CoE object: sub-indices, types, bit offsets, defaults (decoded), limits, enum texts | `hw_object device=EL3356-0010 index=#x8000 [revision=]` — one sub-index: `index=8000:11` (CoE notation, sub-index hex: 17) |
| A diagnosis message (diagnosis history text id, object 0x10F3) | `hw_diag code=#x4101 [device=EL7211-0010]` |
| Which device / object / message / module mentions a word | `hw_search query=… [kinds=device,object,diag,module] [limit=1-100]` (a module is found by its devices too: `query=AX8206 kinds=module`) |
| A module of a modular device (AX8000 slots, TSC) | `hw_module ident=#x006b0064 [device=AX8206-0210-0107]` |

## Rules
1. Documentation → this skill. Live PLC values and types → the ads_* tools.
2. Symbol known → `docs_declaration`. Unknown → `docs_search` → take a `ref` → `docs_page section=`.
   `docs_declaration` gives `kind`, `return_type` (function, method, property), `declaration` with its header and
   the members (`inputs`, `outputs`, `in_out`, `members`, `enum_values`, `methods`, `properties`) as the index parsed
   them — the same answer as `infosys_declaration`; `declaration_blocks` are the page's own code blocks, which often
   lack the header (`FUNCTION MEMCPY : UDINT` is a heading on the page). A method or property of an FB:
   `symbol=FB_WG_ComboFilter.Call` (Beckhoff titles the page `Call`; the Help Viewer contents tie it to its FB).
3. `ambiguous` = the name is documented by different pages (Tc2_MC2 vs Tc3_DriveMotionControl, or two methods of
   that name in one library). Nothing was chosen for you. Find the library the project uses (`proj_libraries` of the
   twincat server, or ask), call again with `book=` or `ref=`, and say in the answer which library you used. Never
   write code against the first candidate by guess. Not ambiguous — the plugin's `infosys_declaration` rule: one
   page in several books (`same_page_in`), the same declaration of one library on several pages, and pages that
   document no IEC symbol (the C++ `AdsRead` beside ADSREAD) when a symbol of that name is asked for.
4. `also_in` lists the other pages of that title or symbol (`same_document: true` = the same page or declaration).
   Mention it when the library matters.
5. Every item has `ref` (book/pageid). Any `ref`, Infosys URL or `ms-xhelp:///?Id=…` link opens with `docs_page`. Follow a
   page's `links` (`text`, `ref`) with `docs_page ref=…`. `parent` / `children` (each with `ref`) are the page's place in
   the Help Viewer contents: an FB's page lists its method and property pages, a method page names its FB.
6. Cite `ref` and `url` in the answer.
7. Queries are FTS5: words are ANDed, `"exact phrase"`, `OR`, `NOT`, `prefix*`. Code with punctuation is fine —
   invalid syntax is retried as a phrase, and `query_mode` says so. A question in plain words falls back step by step
   (`query_mode`): all words → all words found in the index → as prefixes → some words. Stop words, words shorter than
   3 letters and words on no page are dropped first (`words_ignored` gives the reason), and in the some-words step a
   word on more than 20 % of the pages does not pick pages (`words_too_common`); each hit then lists the words it holds
   (`matched`). Nothing informative left → no hits: say so instead of reading unrelated pages.
8. Ranges are refused, not clamped. Read the `message` and correct the call. Do not retry it unchanged.
9. Long pages: `section=` first (the `outline` lists the headings), then `offset=<next_offset>`. Never page through
   a whole book.
10. Nothing found → say so. Then try the twincat server's `infosys_*` tools. A manual that is not indexed: the user
    copies the PDF into `<kb>\manuals\` and runs `manuals_db.py update` (`hint` names the command).
11. Read-only: the script never changes the databases.

## Where the data is
`<kb>` is the knowledge folder of the twincat plugin. It comes from `--kb PATH`, else the environment variable
`TCMCP_KNOWLEDGE_DIR`, else the folder chosen with `kb_set_location`, else `D:\TwinCAT-MCP\knowledge`. A folder
set in `twincat_mcp.toml` (`[knowledge] location`) is not seen by the script: take the exact command from the twincat
server's `kb_status` → `docs.call` (it carries `--kb`). `kb_status` also shows both indexes and the PDFs not
indexed yet.

| Index | Content | Built by |
|---|---|---|
| `<kb>\index\infosys-1033.db` | the TwinCAT 3 books of the Help Viewer package — the same index the twincat server's `infosys_*` tools read | the plugin itself the first time; later `<kb>\tools\infosys_local.py index` or `kb_rebuild(index='infosys')` (minutes) |
| `<kb>\index\manuals.db` | PDF manuals from `<kb>\manuals\`, one row per page, chapters, product codes | `<kb>\tools\manuals_db.py update` or `kb_rebuild(index='manuals')` (Python 3.12+ with PyMuPDF or pypdf) |
| `<kb>\index\esi.db` | the ESI files (`.xml`) in `<kb>\manuals\` and TwinCAT's ESI folders: devices, PDOs, CoE dictionaries, diagnosis messages, modules — the same index the twincat server's `hw_*` tools read | the plugin itself the first time; later `kb_rebuild(index='esi')` or the plugin's `builders\esi_db.py update` (minutes) |

`status`, and `source.state` in every answer, give the state of the index: `complete`; `rebuild_needed` (built by
another format) or `outdated` (the package or the PDFs changed) — both are used as they are; or `missing`. A build
in progress, interrupted or waiting to replace the index is in `state_note`. When it is not `complete`, tell the user
and that the rebuild runs only once they approve it (`kb_rebuild`, or the command in `hint`); the answer may come
from an older package or format.

`no_index` means the index is missing. Tell the user the build command from `hint`. Do not search the web instead
without saying so.
