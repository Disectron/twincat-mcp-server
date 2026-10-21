---
name: twincat-docs
description: Use for ANY Beckhoff documentation question — what a library FB/function/struct/enum does, its inputs/outputs/types (ADSREAD, MC_MoveAbsolute, FB_FileOpen …), error codes (err_lookup first), TwinCAT concepts, code examples, hardware details from manuals (LEDs, wiring, diagnostics of an IPC/terminal), or EtherCAT device data from the ESI files (PDOs, CoE objects with defaults, diagnosis text ids). Looks it up OFFLINE in the local Infosys (Help Viewer package), PDF-manual and ESI knowledge base. Never use ads_* tools for documentation.
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
| **An error code, a code's name or an error message** (`0x745`, `1861`, `16#745`, `0x98110745`, `ADSERR_CLIENT_SYNCTIMEOUT`, `ADSError 1861 (0x745): …`, `Ads-Error 0xA2`, `nErrId := 1808`, `AL status code 0x0011`) | `err_lookup query=… [domain=ads\|win32\|nc\|ethercat_al\|…] [limit=1-20]` — **first**, before docs_search: domain, hex / dec / HRESULT, symbol, Beckhoff's text (every text when the copies of the table differ: `texts` with their books, `main` = the text of the most books; `missing_in` = the copies that lack the code), `ref` + `url` of the page, the plugin's own note (`plugin_note`). The same function as the twincat server's `err_lookup` tool. |
| Inputs/outputs/members of a known FB, function, struct or enum | `docs_declaration symbol=ADSREAD book=tcplclib_tc2_system` (or `library=Tc2_System`, as `infosys_*` take it) |
| Concept, how-to, error text, anything else in Infosys | `docs_search query=… [book=glob] [library=] [limit=1-50] [offset=]` → `ref` → `docs_page ref=… section=…` (the plugin's infosys_search: identifiers count as their words, titles that name the query first, an error code's defining table first — `code_note`, and `code_lookup` from the error index —, one page in several books once — `same_page_in`) |
| A page or a link from an answer | `docs_page ref=<book/pageid, Infosys URL, page id, exact title, ms-xhelp link> [section=] [offset=] [max_chars=200-60000]` |
| Which books exist | `docs_books filter=tcplclib_tc2_*` |
| Hardware: which manual covers a product | `manual_product code=EL3102` (also matches EL3102-xxxx; a variant no manual names, `CX9020-0130`, falls back to the base product — `matched_as` says so; a code the index's product table lacks but its page text names is answered from the text: `matched_as: page text`, `note` says why) |
| Hardware: LEDs, wiring, diagnostics, error codes | `manual_search query=… [doc=CX9020] [limit=1-50] [offset=]` → `manual_page doc=… pages=N-M [offset=]` (≤ 21 pages; `next` / `next_pages` + `next_offset` read on) |
| Chapters of a manual | `manual_toc doc=CX9020 [depth=1-6]` |
| Which manuals are indexed | `manual_docs [filter=…]` |
| EtherCAT device from its ESI: PDOs and entries, PDO assignments, DC modes, slots, CoE object list, the indexed manuals that name it | `hw_lookup query=EL3356-0010 [revision=#x…] [detail=newest\|summary\|full] [limit=1-30]` (a product code `#x0d1c3052` / `0x0d1c3052` / decimal `219951186`, or part of a name works too; a revision that is not indexed: `note` + `revisions_available`) |
| One CoE object: sub-indices, types, bit offsets, defaults (decoded), limits, enum texts | `hw_object device=EL3356-0010 index=#x8000 [revision=]` — one sub-index: `index=8000:11` (CoE notation, sub-index hex: 17) |
| A diagnosis message (diagnosis history text id, object 0x10F3) | `hw_diag code=#x4101 [device=EL7211-0010]` |
| Which device / object / message / module mentions a word | `hw_search query=… [kinds=device,object,diag,module] [limit=1-100] [offset=]` (a module is found by its devices too: `query=AX8206 kinds=module`) |
| A module of a modular device (AX8000 slots, TSC) | `hw_module ident=#x006b0064 [device=AX8206-0210-0107]` |

## Rules
1. Documentation → this skill. Live PLC values and types → the ads_* tools.
   **An error code → `err_lookup` first.** A number counts as a code only as the whole query or next to a word that
   says so (`error 1861`, `nErrId := 1808`) — `ADS error on port 851` holds no code, so it is `documented: false`
   (`text_matches` only share its words: never an answer); a hex number also right after a domain's name
   (`AL status 0x0011`), a code's symbol also in capitals (`WSAETIMEDOUT`), and `C0373` is read as a compiler message.
   The server's err_lookup tool adds, for a question without a code, `from_error_log`: the errors it saw that are of
   a name of the question (a variable `MAIN.fbValve.bOpen` / `fbValve` / `aValves[3]`, a POU, a PLC project —
   compared with the names the call was made with and the names the error quotes; `aValves[5]` is not `aValves[3]`),
   then the XAE Error List for those names; or the newest error when the question says "the last / previous error"
   ("the last ADS error": of that kind; an error without a code is said so; `names_not_found` when no error is of its
   names) — no word of an error is matched. With `online=true` the code the error log found is searched too. `online=true` adds `online`: a site search of infosys.beckhoff.com for the
   question's codes and documented code names only (never its words, project, machine or customer names) — only when
   asked, and pages that mention the code, not a meaning. This command is offline. A number of several domains is
   listed in all of them, ranked by the message's words; a message that names a domain (`ADS error 6`) is answered for
   it and the same number elsewhere comes in `other_domains` — never pick one silently. `documented: false` = not in
   the local documentation (CoE SDO abort codes and the compiler's C0xxx messages have no local source at all —
   `no_local_source`): say so, never guess a meaning; `fulltext` hits only mention the code. `plugin_note` is the
   plugin's own cause / fix, not Beckhoff's text: say so when you use it. An AL status code from a manual that gives
   each code its own section carries that section's fields in `detail` (description, current / resulting state,
   solution) — Beckhoff's text, cite its `ref` page.
2. Symbol known → `docs_declaration`. Unknown → `docs_search` → take a `ref` → `docs_page section=`.
   `docs_declaration` gives `kind`, `return_type` (function, method, property), `declaration` with its header and
   the members (`inputs`, `outputs`, `in_out`, `members`, `enum_values`, `methods`, `properties`) as the index parsed
   them — the same answer as `infosys_declaration`; `declaration_blocks` are the page's own code blocks, which often
   lack the header (`FUNCTION MEMCPY : UDINT` is a heading on the page). A method or property of an FB:
   `symbol=FB_WG_ComboFilter.Call` (Beckhoff titles the page `Call`; the Help Viewer contents tie it to its FB). A
   member the FB does not document itself is looked up where its page says it is defined, then up EXTENDS /
   IMPLEMENTS (`FB_TcMessage.Send` → `I_TcMessage`'s; `defined_in` says so). A symbol documented inside another page
   (RecipeManCommands' methods as `## Method X` sections, a GVL's constants as a table) answers with `section` and a
   declaration reconstructed from the page's tables (`reconstructed`); `docs_page ref=<that name>` opens its section.
   A page whose Syntax section declares another name than its title: when no other page of its library documents
   that name and the page's tables document nothing the header lacks, the header is its declaration (`declared_as`,
   `declared_as_note`: `ActualsActuator` declares `FB_ActuatorActuals`) and the declared name finds the page.
   Otherwise the header is another symbol's (Beckhoff's copy of a neighbour's: `SendMessageEx2` shows
   `METHOD SendMessageEx`) — `syntax_header_note` says why, and the page shows no declaration of its own; read the
   page's tables (`docs_page`) rather than that header.
   A name that only a page title has as a word answers that page (`kind: Page`, `match: title`, `outline`).
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
   `manual_search`: a hex number or CoE index (`0x8000`, `#x8000`, `16#8000`, `8000:11`) is found in any of these
   notations, and a unit symbol (`°C`, `mV/V`, `mA`, `µs`, `kΩ`) as written on the page, not as its letters; `terms`
   says how each was searched.
8. Ranges are refused — read the `message` (it names the way on) and correct the call; do not retry it unchanged.
   The searches are the exception: `docs_search` / `manual_search` use a `limit` above 50 as 50, `hw_search` above
   100 as 100 (`clamped: true`, `clamped_note`); further hits come with `offset=<next_offset>` (`total` counts them).
9. Long pages: `section=` first (the `outline` lists the headings), then `offset=<next_offset>`. Never page through
   a whole book. A manual's pages cut at `max_chars`: `manual_page pages=<next_pages> offset=<next_offset>` (`next`
   names the call).
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
| `<kb>\index\errors.db` | the error codes the three indexes above document: the code tables of the Infosys index (ADS, Win32, NC, the libraries' error-code pages), the AL status codes of the manuals (rows where the table parses, else a page link), the diagnosis text ids of esi.db (linked) — the same index the twincat server's `err_lookup` reads | the plugin itself the first time; later `kb_rebuild(index='errors')` or the plugin's `builders\errors_db.py update` (seconds); it replaces the index only when it passes its checks |

`status`, and `source.state` in every answer, give the state of the index: `complete`; `rebuild_needed` (built by
another format) or `outdated` (the package or the PDFs changed) — both are used as they are; or `missing`. A build
in progress, interrupted or waiting to replace the index is in `state_note`. When it is not `complete`, tell the user
and that the rebuild runs only once they approve it (`kb_rebuild`, or the command in `hint`); the answer may come
from an older package or format.

`no_index` means the index is missing. Tell the user the build command from `hint`. Do not search the web instead
without saying so.
