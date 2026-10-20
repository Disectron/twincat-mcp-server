"""Searching the documentation indexes — one implementation for every reader: the server's infosys_search and the
twincat-docs skill's docs_search / manual_search read the same index and rank with this code, and kb_search uses its
words (0.8.12: N99, N102, N105 — the two readers had two rankings, and neither split an identifier into its words).

  - a question in words falls back in tiers (search_tiers: every word, the informative words, as prefixes, some);
  - identifiers are words too: FB_SocketConnect is 'socket' + 'connect' (ident_words — stored with every page of
    the Infosys index of format 5 in its own FTS column, and used when the hits are ordered);
  - hits are ordered by how much of the query the title names (title_order): the page about the thing first;
  - an error code is looked up where it is defined: a table row that holds it as hex and as decimal (code_forms,
    definition_score) — '0x745' finds the ADS return-code table before pages that only mention the code.

Standard library only: the server, the twincat-docs skill script and the builders import it.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any, Callable, Iterable


class SearchError(Exception):
    """A query that cannot be searched: code (empty_query, bad_query, internal), message, extra fields."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def fold(w: str) -> str:
    """Lower case without accents — how the index's unicode61 tokenizer stores a word ('für' → 'fur')."""
    return "".join(c for c in unicodedata.normalize("NFKD", w.lower()) if not unicodedata.combining(c))


# the words of a sentence around the names: never searched alone, never a prefix (N65, N71). Compared folded.
STOP_WORDS = frozenset(fold(w) for w in """
a an and are as at be but by can cannot do does for from has have how i if in into is it its no not of on or so such
that the their then there these this to was were what when where which while who why will with without you your
der die das den dem des ein eine einer eines einem und oder ist sind wie was wann wo warum mit von zu zum zur im in
am an auf aus bei für nicht kein keine ich es er sie wir ihr
""".split())
COMMON_SHARE = 0.2          # a word on more than this share of the pages does not pick a page in the some-words tier


# ------------------------------------------------------------------------------------------- identifiers as words
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+[0-9]*|[A-Z]+[0-9]*|[0-9]+")


def ident_words(text: str | None) -> list[str]:
    """The words of the identifiers in a text that are made of several (N102, N105): 'FB_SocketConnect' →
    fb, socket, connect; 'sIPAddr' → s, ip, addr; 'Tc3_EventLogger' → tc3, event, logger. An identifier of one word
    ('ADSLOGSTR', 'TON', 'EL3356') adds nothing — the tokenizer has it already. Lower case, each word once."""
    out: list[str] = []
    seen: set[str] = set()
    for ident in _IDENT.findall(text or ""):
        parts = [p for chunk in ident.split("_") if chunk for p in _PART.findall(chunk)]
        if len(parts) < 2:
            continue
        for p in parts:
            w = p.lower()
            if w not in seen:
                seen.add(w)
                out.append(w)
    return out


def title_words(title: str | None) -> str:
    """What the Infosys index (format 5) stores in its `words` column: the identifier words of the page's title."""
    return " ".join(ident_words(title))


# ------------------------------------------------------------------------------------------- query text
_FTS_SYNTAX = re.compile(r'["*:()^]|\b(?:AND|OR|NOT|NEAR)\b')


def query_text(s: str | None) -> str:
    """A query as typed or pasted: a non-breaking space in it is a space (the indexes hold none)."""
    return (s or "").replace("\xa0", " ")


def has_words(q: str) -> bool:
    return bool(re.search(r"[^\W_]", q))


def natural(q: str) -> bool:
    """Words only (no FTS5 operators, quotes, prefixes, column filters, code punctuation): a question in words."""
    return not _FTS_SYNTAX.search(q) and bool(re.fullmatch(r"[\w\s\-./,?!']+", q))


def informative_words(q: str) -> list[str]:
    """The query's words that can pick a page (folded): no stop words, no 1-2 letter words (numbers are kept)."""
    out = []
    for w in re.findall(r"[^\W_]+", query_text(q)):
        f = fold(w)
        if f not in STOP_WORDS and (len(f) >= 3 or f.isdigit()) and f not in out:
            out.append(f)
    return out


# ------------------------------------------------------------------------------------------- FTS statements
def run_fts(con: sqlite3.Connection, sql: str, query: str, extra: list[Any]) -> tuple[list[sqlite3.Row], str, str]:
    """Run an FTS5 statement whose first parameter is the MATCH expression. -> (rows, mode, expression used).
    Invalid FTS5 syntax is retried once as a literal phrase."""
    q = query_text(query).strip()
    if not q:
        raise SearchError("empty_query", "the query is empty")
    if not has_words(q):
        raise SearchError("empty_query", f"the query {query!r} has no searchable word (punctuation is not indexed)")
    try:
        return con.execute(sql, [q, *extra]).fetchall(), "fts5", q
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "no such module" in msg:
            raise SearchError("internal", f"this Python's SQLite has no FTS5 ({msg}); use the python.org Python") from None
        if "locked" in msg or "disk i/o" in msg.lower():
            raise SearchError("internal", f"database busy ({msg}) — is the index being rebuilt right now?") from None
    phrase = '"' + q.replace('"', '""') + '"'
    try:
        return con.execute(sql, [phrase, *extra]).fetchall(), "phrase (input was not valid FTS5 syntax)", phrase
    except sqlite3.OperationalError as exc:
        raise SearchError("bad_query", f"query not usable even as a phrase: {exc}",
                          hint="FTS5 syntax: words, \"exact phrase\", OR, NOT, prefix*") from None


def vocab(con: sqlite3.Connection, fts: str) -> None:
    """The index's own term statistics for term_df (an in-memory fts5vocab table; nothing is written to the file)."""
    try:
        con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_vocab USING fts5vocab(main, '{fts}', 'row')")
    except sqlite3.Error:
        pass


def term_df(con: sqlite3.Connection, word: str, prefix: bool) -> int | None:
    """Pages holding the word (all its tokens: 'T_AmsNetId' is 't' + 'amsnetid'), from the index's own vocabulary
    (vocab); prefix: whether any term starts with it. None when the vocabulary is not available."""
    toks = [t for t in re.split(r"[^\w]|_", fold(word)) if t]
    if not toks:
        return 0
    try:
        dfs = []
        for i, t in enumerate(toks):
            if prefix and i == len(toks) - 1:
                r = con.execute("SELECT MAX(doc) FROM temp.fts_vocab WHERE term >= ? AND term < ?",
                                (t, t + "\U0010ffff")).fetchone()
            else:
                r = con.execute("SELECT doc FROM temp.fts_vocab WHERE term = ?", (t,)).fetchone()
            dfs.append(int(r[0] or 0) if r else 0)
        return min(dfs)
    except sqlite3.Error:
        return None


def page_count(con: sqlite3.Connection, fts: str = "fts") -> int:
    try:
        # the Infosys index's pages_fts reads its text from pages: count the pages themselves
        return int(con.execute("SELECT COUNT(*) FROM " + ("pages" if fts == "pages_fts" else fts)).fetchone()[0])
    except sqlite3.Error:
        return 0


def fts_columns(con: sqlite3.Connection, fts: str) -> list[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({fts})")]
    except sqlite3.Error:
        return []


# bm25 weight of each Infosys FTS column: a word in the title (or in a title identifier's words) says more than one
# in the text; the URL is not searched
INFOSYS_WEIGHTS = {"url": 0.0, "title": 8.0, "text": 1.0, "code": 1.0, "words": 8.0}


def infosys_rank(con: sqlite3.Connection) -> str:
    """bm25(pages_fts, …) with the weights of the columns this index has (a format-4 index has no `words`)."""
    cols = fts_columns(con, "pages_fts") or ["url", "title", "text", "code"]
    return "bm25(pages_fts, " + ", ".join(f"{INFOSYS_WEIGHTS.get(c, 1.0):.1f}" for c in cols) + ")"


def search_tiers(con: sqlite3.Connection, sql: str, count_sql: str, query: str, extra: list[Any],
                 limit: int, title_key: str | None = None, fts: str = "fts",
                 arrange: Callable[[list[Any]], list[Any]] | None = None, title_col: str | None = None,
                 title_sql: str | None = None) -> tuple[list[sqlite3.Row], int, str, str, dict[str, Any]]:
    """N36/N65: a question in words falls back in steps, and only on words that can pick a page:
      1. every word as written (stop words included: a precise query stays precise);
      2. every informative word that occurs in the index — stop words, 1-2 letter words and words on no page dropped;
      3. those words as prefixes (a word is kept when some term starts with it: 'Diagnos' -> diagnostics);
      4. some of them (OR) — without words that are on more than 20 % of the pages.
    Nothing informative left: no hits. N79: a query of nothing but stop words is refused; one word alone ('the') is
    searched as written. title_key: the hits of a tier are ordered by title_order (N105); arrange: instead, a function
    that orders the hits read (more than `limit`). title_col, title_sql (with title_key or arrange): the FTS column of
    the titles and `sql` ordered by the title's length — the hits of the tier that begin with the query or hold its words in
    the title are read as well (title_hits), not only the best by bm25: a page titled as the query is ordered by its
    title wherever bm25 ranks it. FTS5 syntax or code punctuation
    runs as written (run_fts, with the phrase retry). Needs vocab(con, fts) for the term statistics.
    -> (rows, total, mode, expression, info: words used / ignored with the reason)."""
    q = query_text(query).strip()
    if not natural(q):
        rows, mode, expr = run_fts(con, sql, query, extra + [_fetch(limit, title_key or arrange)])
        rows = _arranged(rows, q, title_key, arrange)
        return rows[:limit], con.execute(count_sql, [expr, *extra]).fetchone()[0], mode, expr, {}
    words = [w for w in re.findall(r"[^\W_]+(?:_[^\W_]+)*", q)]
    if not words:
        raise SearchError("empty_query", f"the query {query!r} has no searchable word (punctuation is not indexed)")
    if len(words) > 1 and all(fold(w) in STOP_WORDS for w in words):
        raise SearchError("empty_query", f"the query {query!r} has only stop words ({', '.join(words)}): they are on "
                          "almost every page and pick none — give a name, a product code or specific words")

    def ph(w: str) -> str:
        return '"' + w.replace('"', '""') + '"'
    ignored: dict[str, str] = {}
    informative = []
    for w in words:
        if fold(w) in STOP_WORDS:
            ignored[w] = "stop word"
        elif len(w) < 3 and not w.isdigit():
            ignored[w] = "shorter than 3 letters"
        else:
            informative.append(w)
    pref = []                                  # informative words some term of the index starts with
    for w in informative:
        df = term_df(con, w, True)
        if df is None or df > 0:
            pref.append(w)
        else:
            ignored[w] = "on no page"
    n_pages = page_count(con, fts)
    common = {w for w in pref if n_pages and (term_df(con, w, False) or 0) > COMMON_SHARE * n_pages}
    tiers = [("all words", " ".join(ph(w) for w in words), words)]
    if pref and pref != words:                 # without the stop words / words on no page, exactly as written
        tiers.append(("all words found in the index", " ".join(ph(w) for w in pref), pref))
    if pref:
        tiers.append(("all words (as prefixes)", " ".join(ph(w) + "*" for w in pref), pref))
    if len(pref) > 1:
        some = [w for w in pref if w not in common] or pref     # a word on most pages picks nothing — unless all are
        tiers.append(("some words (no page has all of them)", " OR ".join(ph(w) + "*" for w in some), some))
    for label, expr, used in tiers:
        rows = con.execute(sql, [expr, *extra, _fetch(limit, title_key or arrange)]).fetchall()
        if rows and title_col and title_sql and (title_key or arrange):
            rows = rows + title_hits(con, title_sql, expr, used,
                                     label.endswith("(as prefixes)") or label.startswith("some"), extra, title_col,
                                     {r[0] for r in rows}, q)
        if rows:
            rows = _arranged(rows, q, title_key, arrange)
            info: dict[str, Any] = {"words_used": used}
            if ignored:
                info["words_ignored"] = ignored
            if label.startswith("some words") and common and set(used) != set(pref):
                info["words_too_common"] = sorted(common)
            return rows[:limit], con.execute(count_sql, [expr, *extra]).fetchone()[0], label, expr, info
    info = {"words_used": []}
    if ignored:
        info["words_ignored"] = ignored
    return [], 0, tiers[-1][0], tiers[-1][1], info


def title_hits(con: sqlite3.Connection, sql: str, expr: str, used: list[str], prefix: bool, extra: list[Any],
               title_col: str, have: set[Any], query: str = "") -> list[sqlite3.Row]:
    """The hits of a tier (expr) that the title order must see, read besides the best by bm25, which it sees alone
    otherwise ('Data type' did not find the page titled 'Data type': bm25 rank 685, behind 300 'Data types' pages full
    of 'data type'): the hits whose title begins with the query's words in their order — the page titled as the query
    is one of them however common its words are ('TwinCAT', 'MC', 'Object Method') — then those whose title holds
    every informative word the tier used. They are hits of the tier, so its count is unchanged. sql orders them by
    the title's length — the shortest title that begins with the query is the query ('TwinCAT' before 300
    'TwinCAT …' pages whose text says TwinCAT more often; bm25 cannot tell when every title holds the word). Rows
    already read (`have`, by the first column) are left out; at most _TITLE_HITS of each."""
    def q_(w: str) -> str:
        return w.replace('"', '""')
    conds = []
    toks = re.findall(r"[^\W_]+", query_text(query))
    if toks:
        conds.append(f'{title_col} : ^"{q_(" ".join(toks))}"')
    words = [w for w in used if fold(w) not in STOP_WORDS and (len(w) >= 3 or w.isdigit())]
    if words:
        star = "*" if prefix else ""
        conds.append(" AND ".join(f'{title_col} : "{q_(w)}"{star}' for w in words))
    out: list[sqlite3.Row] = []
    seen = set(have)
    for cond in conds:
        try:
            rows = con.execute(sql, [f"({expr}) AND ({cond})", *extra, _TITLE_HITS]).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                out.append(r)
    return out


_TITLE_HITS = 300


def _fetch(limit: int, ordered: Any) -> int:
    # the hits are re-ordered (by their titles): more than `limit` are read so that the best titles are among them
    return min(max(limit * 3, 150), 300) if ordered else limit


def _arranged(rows: list[Any], q: str, title_key: str | None, arrange: Callable[[list[Any]], list[Any]] | None) -> list[Any]:
    if arrange is not None:
        return arrange(rows)
    return title_order(rows, q, title_key) if title_key else rows


def matched_words(con: sqlite3.Connection, rowid: int, words: list[str], fts: str = "fts") -> list[str]:
    """Which of the words (as prefixes) the page with this fts rowid holds — shown per hit in the some-words tier."""
    out = []
    for w in words:
        try:
            if con.execute(f"SELECT 1 FROM {fts} WHERE rowid = ? AND {fts} MATCH ?",
                           (rowid, '"' + w.replace('"', '""') + '"*')).fetchone():
                out.append(w)
        except sqlite3.Error:
            pass
    return out


# ------------------------------------------------------------------------------------------- order by title
def _same_word(a: str, b: str) -> bool:
    return a == b or a == b + "s" or b == a + "s" or a == b + "es" or b == a + "es"


def _title_terms(title: str) -> tuple[set[str], int]:
    """(every word a title answers to — its tokens as the index has them and its identifiers' words —, how many
    words the title has: an identifier counts as its words)."""
    own = {fold(t) for t in re.findall(r"[^\W_]+", title)}
    words: list[str] = []
    for tok in re.findall(r"\w+", title):
        parts = ident_words(tok)
        words += parts if parts else [fold(x) for x in tok.split("_") if x]
    return own | set(words), len(set(words)) or 1


def _word_seq(s: str) -> tuple[str, ...]:
    return tuple(fold(w) for w in re.findall(r"[^\W_]+", s or ""))


def title_order(rows: list[Any], query: str, title_key: str = "title", library_key: str | None = None) -> list[Any]:
    """The page whose title is the query — its words in its order, nothing else ('Internal cycle process' before
    'Process (internal cycle)') — first. N105: then the page whose title names most of the query's informative
    words ('tcp client connect socket' →
    FB_SocketConnect: socket, connect), then — library_key — the page whose title and library together name most
    (the library is part of a symbol's name: 'EtherCAT slave state' → the slave-state symbols of Tc2_EtherCAT before
    'State Machine Slave Settings'), then the title that is exactly one of the query's words ('ADSREAD xyzqq' → ADSREAD
    before AdsReadDeviceInfo), then the title the query names most completely; the index's own order (bm25)
    otherwise. A title's words: its own and its identifiers' (ident_words)."""
    q = query_text(query)
    want = informative_words(q)
    raw = {fold(w) for w in re.findall(r"\S+", q)}
    seq = _word_seq(q)
    keyed = []
    for i, r in enumerate(rows):
        title = str(r[title_key] or "")
        terms, size = _title_terms(title)
        cov = sum(1 for w in want if any(_same_word(t, w) for t in terms))
        lib = _title_terms(str(r[library_key] or ""))[0] if library_key else set()
        with_lib = sum(1 for w in want if any(_same_word(t, w) for t in terms | lib)) if lib else cov
        exact = fold(title.strip()) in raw
        whole = bool(seq) and _word_seq(title) == seq
        keyed.append(((not whole, -cov, -with_lib, not exact, -cov / size, i), r))
    keyed.sort(key=lambda x: x[0])
    return [r for _, r in keyed]


# ------------------------------------------------------------------------------------------- error codes (N99)
_HEX = re.compile(r"^(?:0x|16#|#x)([0-9a-f_]+)$", re.I)
# words that make a decimal number of the query an error / return code
_CODE_WORDS = frozenset("""error errors err errid nerrid errorid errorcode errcode errno code codes return returncode
result hresult adserr adserror fehler fehlercode fehlernummer""".split())


def code_forms(query: str) -> dict[str, Any] | None:
    """An error / return code in the query, in the forms the code tables write it (N99): '0x745' → value 1861, hex
    0x745 / 0x0745 / 0x00000745, decimal 1861. One code token in the query: hex (0x…, 16#…, #x…), or a decimal number
    of two digits or more next to a word that says it is a code ('error 1861', 'ADS return code 1861' — 'ADS port
    851' or 'build 4024' is no code, review 0.8.12); else None."""
    q = query_text(query)
    toks = list(re.finditer(r"(?:0x|16#|#x)[0-9a-fA-F_]+|\b\d{2,}\b", q))
    if len(toks) != 1:
        return None
    t = toks[0].group(0)
    m = _HEX.match(t)
    if not m and not _next_to_code_word(q, toks[0].start(), toks[0].end()):
        return None
    try:
        value = int(m.group(1).replace("_", ""), 16) if m else int(t)
    except ValueError:
        return None
    if value < 16:
        return None
    h = f"{value:x}"
    hexes = sorted({f"0x{h}", f"0x{h.zfill(4)}", f"0x{h.zfill(8)}"}, key=len)
    return {"token": t, "value": value, "hex": hexes, "dec": str(value)}


def _next_to_code_word(q: str, start: int, end: int) -> bool:
    """The decimal number q[start:end] stands alone (not part of a name: 'P-CHAN-00020', 'EL3102_1') and the word
    right before or after it says it is a code ('error 1861', 'nErrId = 1861', '1861 error')."""
    if re.search(r"[\w-]$", q[:start]) or re.match(r"[\w-]", q[end:]):
        return False
    before = re.search(r"([^\W\d_]+)[\s:=#(]{1,3}$", q[:start])
    after = re.match(r"[\s:)]{1,3}([^\W\d_]+)", q[end:])
    return any(x is not None and fold(x.group(1)) in _CODE_WORDS for x in (before, after))


def _cell_value(cell: str) -> tuple[str, int] | None:
    c = cell.strip().strip("`*").strip()
    m = _HEX.match(c)
    if m:
        try:
            return "hex", int(m.group(1).replace("_", ""), 16)
        except ValueError:
            return None
    if re.fullmatch(r"\d+", c):
        return "dec", int(c)
    return None


def definition_score(markdown: str | None, value: int) -> int:
    """How a page holds the code: 2 = a table row with the code as hex and as decimal in cells of their own (a code
    table: '| 0x745 | 1861 | … | ADSERR_CLIENT_SYNCTIMEOUT |'), 1 = a table cell that is the code, 0 = a mention."""
    best = 0
    for ln in (markdown or "").splitlines():
        if not ln.startswith("|"):
            continue
        forms = {f for f, v in filter(None, (_cell_value(c) for c in ln.strip().strip("|").split("|"))) if v == value}
        if {"hex", "dec"} <= forms:
            return 2
        if forms:
            best = 1
    return best


def code_definitions(con: sqlite3.Connection, query: str, sql: str, extra: list[Any], body: Callable[[Any], str],
                     limit: int = 150) -> tuple[dict[str, Any] | None, list[Any]]:
    """N99: the pages that define the code of the query: `sql` (its first parameter the MATCH expression, its last
    the row limit) run for the pages that hold the code as hex AND as decimal, kept when a table row holds both
    (definition_score 2). -> (code_forms, rows in the statement's order)."""
    forms = code_forms(query)
    if forms is None:
        return None, []
    expr = "(" + " OR ".join(f'"{h}"' for h in forms["hex"]) + f') AND "{forms["dec"]}"'
    try:
        rows = con.execute(sql, [expr, *extra, limit]).fetchall()
    except sqlite3.Error:
        return forms, []
    return forms, [r for r in rows if definition_score(body(r), forms["value"]) == 2]


def first_of_each(rows: Iterable[Any], key: Callable[[Any], Any]) -> list[Any]:
    seen: set[Any] = set()
    out = []
    for r in rows:
        k = key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out
