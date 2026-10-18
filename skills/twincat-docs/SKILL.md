---
name: twincat-docs
description: Use for ANY Beckhoff documentation question — what a library FB/function/struct/enum does, its inputs/outputs/types (ADSREAD, MC_MoveAbsolute, FB_FileOpen …), error codes, TwinCAT concepts, code examples, or hardware details from manuals (LEDs, wiring, diagnostics of an IPC/terminal). Looks it up OFFLINE in the local Infosys (Help Viewer package) and PDF-manual knowledge base. Never use ads_* tools for documentation.
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
| Concept, how-to, error text, anything else in Infosys | `docs_search query=… [book=glob] [limit=1-50]` → `ref` → `docs_page ref=… section=…` |
| A page or a link from an answer | `docs_page ref=<book/pageid, Infosys URL, page id, exact title, ms-xhelp link> [section=] [offset=] [max_chars=200-20000]` |
| Which books exist | `docs_books filter=tcplclib_tc2_*` |
| Hardware: which manual covers a product | `manual_product code=EL3102` (also matches EL3102-xxxx) |
| Hardware: LEDs, wiring, diagnostics, error codes | `manual_search query=… [doc=CX9020]` → `manual_page doc=… pages=N-M` (≤ 21 pages) |
| Chapters of a manual | `manual_toc doc=CX9020 [depth=1-6]` |
| Which manuals are indexed | `manual_docs [filter=…]` |

## Rules
1. Documentation → this skill. Live PLC values and types → the ads_* tools.
2. Symbol known → `docs_declaration`. Unknown → `docs_search` → take a `ref` → `docs_page section=`.
3. `ambiguous` = the name exists in several books (Tc2_MC2 vs Tc3_DriveMotionControl, a PLC library vs the C++
   docs). Nothing was chosen for you. Find the library the project uses (`proj_libraries` of the twincat server, or
   ask), call again with `book=` or `ref=`, and say in the answer which library you used. Never write code against
   the first candidate by guess.
4. `also_in` lists other books with a page of the same title. Mention it when the library matters.
5. Every item has `ref` (book/pageid). Any `ref`, Infosys URL or `ms-xhelp:///?Id=…` link opens with `docs_page`. Follow a
   page's `links` (`text`, `ref`) with `docs_page ref=…`.
6. Cite `ref` and `url` in the answer.
7. Queries are FTS5: words are ANDed, `"exact phrase"`, `OR`, `NOT`, `prefix*`. Code with punctuation is fine —
   invalid syntax is retried as a phrase, and `query_mode` says so.
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
server's `kb_status` → `docs.call` (it carries `--kb`). `kb_status` also shows both databases and the PDFs not
indexed yet.

| Database | Content | Built by |
|---|---|---|
| `<kb>\helpviewer\infosys_en.db` | all Infosys pages of the Help Viewer package (≈ 51,000 pages, 283 books) | `<skill folder>\builders\infosys_local.py index --kb <kb>` (Python 3.12+; ≈ 150 s; a no-op when the package is unchanged) |
| `<kb>\manualsdb\manuals.db` | PDF manuals from `<kb>\manuals\`, one row per page, chapters, product codes | `<skill folder>\builders\manuals_db.py update --kb <kb>` (Python 3.12+; needs PyMuPDF or pypdf) |

`status`, and `source.state` in every answer, say whether the index is `complete`, `outdated` (the package changed),
`old_format`, `incomplete`, `missing`, or being rebuilt (`state_note`). When it is not `complete`, tell the user; the
answer may come from an older package.

`no_index` means the database is missing. Tell the user the build command from `hint`. Do not search the web
instead without saying so.
