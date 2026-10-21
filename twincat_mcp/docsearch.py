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
GERMAN_STOP_WORDS = frozenset(fold(w) for w in """
der die das den dem des ein eine einer eines einem und oder ist sind wie was wann wo warum mit von zu zum zur im in
am an auf aus bei für nicht kein keine ich es er sie wir ihr
""".split()) - frozenset(fold(w) for w in "in an am was".split())
COMMON_SHARE = 0.2          # a word on more than this share of the pages does not pick a page in the some-words tier
# N148 (0.8.14 retest): IEC 61131-3 keywords that are English stop words or shorter than 3 letters — 'THIS pointer'
# dropped THIS (the page THIS could not be found), 'FOR loop BY step' dropped FOR and BY
IEC_KEYWORDS = frozenset("""
FOR TO BY DO OF IF THEN ELSE ELSIF CASE WHILE REPEAT UNTIL EXIT RETURN THIS SUPER AND OR NOT XOR MOD AT IN ON ST
""".split())
STOP_WORDS_UPPER = frozenset(w.upper() for w in STOP_WORDS)
# those a lower-case query is told about when they are dropped (not the small words of every English sentence)
_KEYWORD_HINT = frozenset("THIS SUPER FOR WHILE REPEAT UNTIL CASE EXIT RETURN ELSIF MOD XOR".split())


def keyword(w: str) -> bool:
    """N148: a word written in capitals — an IEC keyword (FOR, BY, THIS, ST) or an acronym of the documentation (AL,
    RT, DC, ADS): searched as a word, never a stop word or a too-short word. Two letters at least."""
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]*", w)) and sum(c.isalpha() for c in w) >= 2


def ignore_reason(w: str) -> str | None:
    """Why a word of a question in words is not searched alone (None: it is): a stop word, a word of 1-2 letters —
    not a word written in capitals (keyword). The reason says how to search an IEC keyword written in lower case."""
    if keyword(w):
        return None
    f = fold(w)
    if f in STOP_WORDS:
        why = "stop word" + (" (German)" if f in GERMAN_STOP_WORDS else "")
        if w.upper() in _KEYWORD_HINT:
            why += f" — the IEC keyword is searched when written in capitals ({w.upper()})"
        return why
    if len(w) < 3 and not w.isdigit():
        return "shorter than 3 letters" + (f" — written in capitals it is searched ({w.upper()})"
                                           if w.upper() in IEC_KEYWORDS - STOP_WORDS_UPPER else "")
    return None


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
    """The query's words that can pick a page (folded): no stop words, no 1-2 letter words (numbers are kept) —
    a word written in capitals always (N148: FOR, BY, THIS, ST)."""
    out = []
    for w in re.findall(r"[^\W_]+", query_text(q)):
        f = fold(w)
        if ignore_reason(w) is None and f not in out:
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
                 arrange: Callable[..., list[Any]] | None = None, title_col: str | None = None,
                 title_sql: str | None = None, skip: int = 0) -> tuple[list[sqlite3.Row], int, str, str, dict[str, Any]]:
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
    skip (N167, paging): the hits up to skip + limit are returned, the caller shows those after `skip`. The title order
    sees the same hits whatever the page — the best _fetch(limit) by bm25 and their title hits — and the hits after
    those follow in bm25 order (arrange(rows, tail)): a page never repeats or skips a hit of another.
    -> (rows, total, mode, expression, info: words used / ignored with the reason)."""
    # N149 (0.8.14 retest): ST's dereference 'THIS^' / 'pRef^' is no FTS5 initial-token operator ('^word'); the
    # index has no punctuation — it is the word
    q = re.sub(r"(?<=\w)\^+", "", query_text(query)).strip()
    ordered = title_key or arrange
    head_n = _fetch(limit, ordered)                # the hits the title order sees: the same for every page (N167)
    want = head_n + (skip + limit) * 3 if skip and ordered else max(head_n, skip + limit)
    if not natural(q):
        rows, mode, expr = run_fts(con, sql, q, extra + [want])
        if not rows:
            # N149 (0.8.14 retest): 'THIS^ SUPER^' / 'set reset S= R=' — as written (a phrase) nothing, and nothing
            # else was tried: its words are searched as a question in words
            plain = re.sub(r'[^\w\s]|\b(?:AND|OR|NOT|NEAR)\b', " ", q)
            if has_words(plain) and plain.split() != q.split():
                got = search_tiers(con, sql, count_sql, plain, extra, limit, title_key, fts, arrange, title_col,
                                   title_sql, skip)
                if got[0]:
                    info = dict(got[4])
                    info["as_written"] = f"{q!r} as written ({mode}) found nothing — its words were searched"
                    return got[0], got[1], f"words of the query ({got[2]})", got[3], info
        rows = _arranged(rows[:head_n] if ordered else rows, q, title_key, arrange, rows[head_n:] if ordered else [])
        return rows[:skip + limit], con.execute(count_sql, [expr, *extra]).fetchone()[0], mode, expr, {}
    words = [w for w in re.findall(r"[^\W_]+(?:_[^\W_]+)*", q)]
    if not words:
        raise SearchError("empty_query", f"the query {query!r} has no searchable word (punctuation is not indexed)")
    if len(words) > 1 and all(fold(w) in STOP_WORDS and not keyword(w) for w in words):
        raise SearchError("empty_query", f"the query {query!r} has only stop words ({', '.join(words)}): they are on "
                          "almost every page and pick none — give a name, a product code or specific words (an IEC "
                          "keyword in capitals: FOR, THIS)")

    def ph(w: str) -> str:
        return '"' + w.replace('"', '""') + '"'
    ignored: dict[str, str] = {}
    informative = []
    for w in words:
        why = ignore_reason(w)
        if why:
            ignored[w] = why
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
    kws = [w for w in pref if keyword(w)]
    if len(pref) > 1:
        # a word on most pages picks nothing — unless all are; N148: nor a keyword in capitals (THIS is 'this' in the
        # index, on most pages — the pages titled THIS are found through their titles, title_hits)
        some = [w for w in pref if w not in common or w in kws] or pref
        tiers.append(("some words (no page has all of them)", " OR ".join(ph(w) + "*" for w in some), some))
    for label, expr, used in tiers:
        rows = con.execute(sql, [expr, *extra, want]).fetchall()
        tail = rows[head_n:] if ordered else []
        rows = rows[:head_n] if ordered else rows
        if rows and title_col and title_sql and ordered:
            rows = rows + title_hits(con, title_sql, expr, used,
                                     label.endswith("(as prefixes)") or label.startswith("some"), extra, title_col,
                                     {r[0] for r in rows}, q, [w for w in kws if w in used])
            have = {r[0] for r in rows}
            tail = [r for r in tail if r[0] not in have]
        if rows:
            rows = _arranged(rows, q, title_key, arrange, tail)
            info: dict[str, Any] = {"words_used": used}
            # N166 (0.8.14 retest): the first tier searches every word as written — a word it used is not ignored
            # ('AL status codes': words_used AL and words_ignored AL 'shorter than 3 letters')
            left = {w: why for w, why in ignored.items() if w not in used}
            if left:
                info["words_ignored"] = left
            if label.startswith("some words") and common and set(used) != set(pref):
                info["words_too_common"] = sorted(common)
            return rows[:skip + limit], con.execute(count_sql, [expr, *extra]).fetchone()[0], label, expr, info
    info = {"words_used": []}
    if ignored:
        info["words_ignored"] = ignored
    return [], 0, tiers[-1][0], tiers[-1][1], info


def title_hits(con: sqlite3.Connection, sql: str, expr: str, used: list[str], prefix: bool, extra: list[Any],
               title_col: str, have: set[Any], query: str = "", keywords: Iterable[str] = ()) -> list[sqlite3.Row]:
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
    words = [w for w in used if ignore_reason(w) is None]
    if words:
        star = "*" if prefix else ""
        conds.append(" AND ".join(f'{title_col} : "{q_(w)}"{star}' for w in words))
    # N148: the pages whose title has a keyword of the query ('THIS', 'FOR') — a common word in the text
    conds += [f'{title_col} : "{q_(k)}"' for k in keywords]
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


def _arranged(rows: list[Any], q: str, title_key: str | None, arrange: Callable[..., list[Any]] | None,
              tail: list[Any] | None = None) -> list[Any]:
    """The hits in their order: `rows` by their titles (or arrange), then `tail` (the hits after those the title
    order sees, N167) in bm25 order — arrange(rows, tail) also merges the tail's copies."""
    if arrange is not None:
        return arrange(rows, tail or [])
    return (title_order(rows, q, title_key) if title_key else rows) + list(tail or [])


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


# a word of its own: not part of an identifier (AXIS_REF), not after a sigil ($FOR of the CNC, #x, %I)
_CAPS_TOKEN = re.compile(r"(?<![\w$#%@&])[A-Za-z][A-Za-z0-9]*(?![\w])")


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
    tk = _title_keyer(query)
    keyed = [(tk(str(r[title_key] or ""), str(r[library_key] or "") if library_key else "")[0] + (i,), r)
             for i, r in enumerate(rows)]
    keyed.sort(key=lambda x: x[0])
    return [r for _, r in keyed]


# N183 (0.8.15 retest): the order title_order gives, said per hit — 'ADSREAD xyzqq' listed ADSREAD (score 12.85) before
# AdsRead (14.98): the title rule placed it, and the score shown is the text's bm25, which the order does not follow
ORDER_NOTE = ("order: the pages whose table defines a queried code first (code_note), then the pages whose title "
              "matches the query by title_rank (1 = the closest match; title_match says how), then the others by "
              "score (bm25 relevance of the page text, higher = better) — so an item can have a lower score than the "
              "one after it")


def title_ranks(rows: list[Any], query: str, title_key: str = "title", library_key: str | None = "library",
                skip: set[Any] | None = None, id_key: str = "rid") -> dict[Any, tuple[int, str]]:
    """N183: {row id: (title_rank, how its title matched)} for the rows whose title placed them (title_order's rule;
    1 = the closest match among these rows; equal ranks keep the index's order); `skip`: ids placed otherwise."""
    tk = _title_keyer(query)
    keyed = [(r[id_key], *tk(str(r[title_key] or ""), str(r[library_key] or "") if library_key else ""))
             for r in rows if r[id_key] not in (skip or set())]
    keyed = [(rid, key, how) for rid, key, how in keyed if how]
    order = {key: n for n, key in enumerate(sorted({key for _rid, key, _how in keyed}), 1)}
    return {rid: (order[key], how) for rid, key, how in keyed}


def _title_keyer(query: str) -> Callable[[str, str], tuple[tuple[Any, ...], str | None]]:
    """title_order's rule: (title, library) -> (sort key, how the title matched or None)."""
    q = query_text(query)
    want = informative_words(q)
    raw = {fold(w) for w in re.findall(r"\S+", q)}
    seq = _word_seq(q)
    # review of 0.8.15: an IEC keyword, not every word in capitals — 'PLC persistent variables' put 'Resetting the PLC
    # project' above 'Remanent Variables - PERSISTENT, RETAIN', 'CX9020 LED' lost 'Diagnostic LEDs': an acronym of
    # the query is a word like any other (it is searched, keyword()); only a keyword names the page it titles
    caps = {w for w in _CAPS_TOKEN.findall(q) if w in IEC_KEYWORDS}
    # N149: an operator of the query as written — a word with its punctuation ('REF=', 'S=', 'THIS^') — that a title
    # holds as one of its tokens: the page of that operator ('Assignment operator REF=', not AXIS_REF)
    ops = {t for t in q.split() if re.search(r"\w", t) and re.search(r"[^\w\s]", t)}

    def key(title: str, library: str) -> tuple[tuple[Any, ...], str | None]:
        terms, size = _title_terms(title)
        cov = sum(1 for w in want if any(_same_word(t, w) for t in terms))
        lib = _title_terms(library)[0] if library else set()
        with_lib = sum(1 for w in want if any(_same_word(t, w) for t in terms | lib)) if lib else cov
        exact = fold(title.strip()) in raw
        whole = bool(seq) and _word_seq(title) == seq
        # N148: a keyword of the query written in capitals that the title has as a word of its own, in capitals —
        # 'ST instruction FOR' for 'FOR loop BY step' (not the CNC's '$FOR'), 'THIS' for 'THIS pointer'
        kw = (len(caps & set(_CAPS_TOKEN.findall(title))) if caps else 0) + (len(ops & set(title.split())) if ops else 0)
        # the last rule (how much of the title the query names) is said too: ADSREAD (one word) before AdsRead (two)
        share = "" if cov == size else f"; {cov} of the title's {size} words"
        how = ("the title is the query" if whole else
               "the title has the query's keyword / operator" if kw else
               ("the title is the query's word" if exact else f"the title names {cov} of the query's {len(want)} "
                "words") + ("" if with_lib == cov else f" ({with_lib} with its library)") + share if cov else
               f"its library names {with_lib} of the query's {len(want)} words" if with_lib else
               "the title is one of the query's words" if exact else None)
        return (not whole, -kw, -cov, -with_lib, not exact, -cov / size), how
    return key


# ------------------------------------------------------------------------------------------- error codes (N99)
_HEX = re.compile(r"^(?:0x|16#|#x)([0-9a-f_]+)$", re.I)
# Which numbers of a question are codes (N99, N121, N122, rounds 7-10 of the 0.8.16 review). One rule set, read
# against one corpus of questions (tests/fixtures/code_questions.tsv) — each round's fix of one phrasing had broken
# others ('the 1861 error', '1861 error, 3 times', 'axis 0x4650 error', 'Build failed with 1 error'):
#   - a code word right before the number names it: 'error 1861', 'error: 1861', 'error - 1861', 'error, 0x745',
#     'nErrId := 1861', "AL Status Code: '0x001b'", two words: 'error number 1861', 'return value 1861';
#   - a code word right after it (a space between) names it: '1861 error', 'the 0x745 error' — not a number of one
#     digit ('1 error', 'retry 3 error': a count), not a number a thing's name stands before ('Term 4 error',
#     'Port 851 error'; a hex number is no thing's number, and four digits are no term's, axis' or device's:
#     'axis 4650 error'), not when the code word names a code of its own after it ('Task 1 error 1861');
#   - a count is no code: a decimal followed by what it counts ('Error: 2 axes not referenced', 'error 3 times',
#     '1 error, 3 warnings' — up to three digits: 'error 1808 symbols not found' is 1808), a position ('Error 1 of 3'),
#     a plural code word after it ('3 errors', '1 error(s)', '3 error messages' — not after a list of codes: '1861
#     and 1808 errors'), a decimal after 'errors' ('errors: 3'; four digits name codes: 'errors 1861 and 1808');
#   - a number listed with a code ('error 1861 or 1808', 'errors 1861 and 1808') is one too — two digits at least.
# A hex number is never a count or a thing's number.
_CODE_WORDS = frozenset("""error errors err errid nerrid errorid nerrorid errorcode nerrorcode errcode errno errnum
code codes return returncode result hresult adserr adserror fehler fehlercode fehlernummer""".split())
# 'error number 1861', 'error ID 1861', 'Fehler Nr. 1861', 'return value 1861': a code word and one of these
_CODE_WORD_SUFFIX = frozenset("number id no nr code nummer value wert".split())
_COUNT_WORDS = frozenset("errors codes results warnings fehler".split())   # after a number: how many, not which one
_SAME_PLURAL = frozenset({"fehler"})          # a plural like its singular: '3 Fehler' counts, '1861 Fehler' names
# what a number counts when it stands right before it ('2 axes', '3 times', '3 warnings', '2 Achsen')
_COUNT_NOUNS = frozenset("""times mal retries tries attempts axes achsen terminals klemmen symbols symbole variables
variablen seconds secs sekunden minutes minuten hours stunden days tage ms warnings warnungen errors messages meldungen
devices geräte drives antriebe boxes slaves items entries values werte cycles zyklen tasks channels kanäle modules
steps schritte lines zeilen points objects objekte codes fehler hinweise infos""".split())
# words a number after which is that thing's number, not a code ('Term 4 error', 'Port 851 error', 'Line 34 error'):
# TwinCAT's own tree item names and the things logs number — a port's, a line's and a step's number of any size
_NUMBERED = frozenset("""term box device drive axis module channel coupler slot task port line step item terminal slave
node station klemme kanal achse zeile""".split())
_NUMBERED_ANY = frozenset("port line zeile step".split())
_COUNT_BEFORE = frozenset("errors".split())                                # 'errors: 3' — before it as well
_LISTED = re.compile(r"\s*(?:,|/|&|\band\b|\bor\b|\bvs\b\.?|\bversus\b|\bund\b|\boder\b)\s*\Z", re.I)


def code_tokens(query: str, min_digits: int = 2, hex_anywhere: bool = True) -> list[dict[str, Any]]:
    """The code tokens of a query, by the rules of code_forms (N99, N121, N122) — one implementation for the docs
    search and the error index's err_lookup (N125). A hex number (0x…, 16#…, #x…) is a code anywhere when hex_anywhere,
    else only as the whole query or next to a word that says it is a code; a decimal number of min_digits digits or more
    (a minus sign right before it belongs to it: '-6908') only as the whole query ('1861', '1808?') or next to such a
    word ('error 1861', 'nErrId := 1808', '1861 error' — 'ADS error on port 851' is no code 851, N121), or listed with
    such a number ('error 1861 or 1808'). -> [{token, value, how: 'hex' | 'dec', whole, next_to_code_word, start,
    end}] in the order of the query."""
    q = query_text(query)
    found: list[dict[str, Any]] = []
    # fourth review of 0.8.16: '0X98110745' (a capital X) is a hex number too
    bare = q.strip().rstrip("?!.").strip()
    for tk in re.finditer(r"(?:0[xX]|16#|#[xX])[0-9a-fA-F_]+|(?:(?<![\w.\-])-)?\b\d{%d,}\b" % max(1, int(min_digits)), q):
        t = tk.group(0)
        m = _HEX.match(t)
        try:
            value = int(m.group(1).replace("_", ""), 16) if m else int(t)
        except ValueError:
            continue
        found.append({"token": t, "value": value, "how": "hex" if m else "dec", "whole": bare == t,
                      "next_to_code_word": _next_to_code_word(q, tk.start(), tk.end()), "start": tk.start(),
                      "end": tk.end()})
    # tenth review of 0.8.16: a number listed with a code is one ('error 1861 or 1808'); not a count, not one digit
    changed = True
    while changed:
        changed = False
        for i, c in enumerate(found):
            if c["next_to_code_word"] or (c["how"] == "dec" and len(c["token"].lstrip("-")) < 2) or \
                    _counted(q, c["start"], c["end"]) or _attached(q, c["start"], c["end"]) or \
                    _things_number(q, c["start"], c["end"]):
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(found) and found[j]["next_to_code_word"]:
                    a, b = (found[j], c) if j < i else (c, found[j])
                    if _LISTED.match(q[a["end"]:b["start"]]):
                        c["next_to_code_word"] = changed = True
                        break
    return [c for c in found if c["whole"] or c["next_to_code_word"] or (c["how"] == "hex" and hex_anywhere)]


def code_forms(query: str) -> dict[str, Any] | None:
    """An error / return code in the query, in the forms the code tables write it (N99): '0x745' → value 1861, hex
    0x745 / 0x0745 / 0x00000745, decimal 1861. One code token in the query: hex (0x…, 16#…, #x…), or a decimal number
    of two digits or more that is the whole query ('1861', N122) or stands next to a word that says it is a code
    ('error 1861', 'ADS return code 1861' — 'ADS port 851' or 'build 4024' is no code, review 0.8.12); else None."""
    # N122 (0.8.13 retest): a number that IS the query ('1861') is a code too, as the tool describes it — inside other
    # words a decimal needs a code word next to it ('error 1861'; 'ADS error on port 851' is no code 851, N121); a hex
    # number is a code. Review of 0.8.14: the query's other numbers do not cancel it ('ADS error 1861 on port 851',
    # 'ADS error 0x745 (1861)' — one code, written twice); two different codes are none
    # re-review of 0.8.16: a negative number that IS the query ('-6908', LabVIEW's 0xFFFFE504) is a code too
    cands = [(c["token"], c["value"]) for c in code_tokens(query)
             if not c["token"].startswith("-") or c["whole"] or c["next_to_code_word"]]       # 'error -6908' too
    if len({v for _t, v in cands}) != 1:
        return None
    t, value = cands[0]
    if 0 <= value < 16:
        return None
    h = f"{value & 0xFFFFFFFF:x}"
    hexes = sorted({f"0x{h}", f"0x{h.zfill(4)}", f"0x{h.zfill(8)}"}, key=len)
    return {"token": t, "value": value, "hex": hexes, "dec": str(value)}


def _attached(q: str, start: int, end: int) -> bool:
    """Part of a name, not a number of its own: 'P-CHAN-00020', 'EL3102_1', 'error1861', 'error-1861'."""
    return bool(re.search(r"[\w-]$", q[:start]) or re.match(r"[\w-]", q[end:]))


def _counted(q: str, start: int, end: int) -> bool:
    """The decimal q[start:end] says how many, or which one of several: a position ('Error 1 of 3', '4 of 10
    symbols'), or a number of up to three digits right before what it counts ('2 axes', '3 times', '3 warnings',
    '3 warning(s)'). A hex number never counts."""
    if _HEX.match(q[start:end]):
        return False
    if re.match(r"[ \t]+(?:of|von)[ \t]+\d", q[end:], re.I):
        return True
    m = re.match(r"[ \t]{1,3}([^\W\d_]+)(\(s\))?", q[end:])
    return bool(m and len(q[start:end].lstrip("-")) <= 3 and (fold(m.group(1)) in _COUNT_NOUNS or m.group(2)))


def _things_number(q: str, start: int, end: int) -> bool:
    """The decimal q[start:end] is the number of the thing named right before it ('Term 4', 'Port 851', 'line 1861';
    four digits are no term's, axis' or device's: 'axis 4650')."""
    tok = q[start:end]
    if _HEX.match(tok):
        return False
    thing = re.search(r"(?i)\b(" + "|".join(sorted(_NUMBERED)) + r")\.?[ \t]+$", q[:start])
    return bool(thing and (thing.group(1).lower() in _NUMBERED_ANY or len(tok.lstrip("-")) <= 3))


def _next_to_code_word(q: str, start: int, end: int) -> bool:
    """The number q[start:end] stands alone (_attached) and a word right before or after it says it is a code — the
    rules above code_tokens."""
    if _attached(q, start, end) or _counted(q, start, end):
        return False
    tok = q[start:end]
    dec = not _HEX.match(tok)
    digits = len(tok.lstrip("-"))
    # before: the code word, then a space, ':', '=', '#', '(', a quote, a dash, a comma or a semicolon ('error 1861',
    # 'nErrId := 1861', "AL Status Code: '0x001b'", 'error – 0x4260', 'I got an error, 1861')
    before = re.search(r"([^\W\d_]+)([\s:=#('\"‘“\-–—,;]{1,6})$", q[:start])
    if before is not None:
        w = fold(before.group(1))
        # after a comma or a semicolon one digit is too little ('an error, 2 PLCs down' — a count of what it names)
        loose = dec and digits < 2 and re.search(r"[,;]", before.group(2))
        if w in _CODE_WORDS and not (dec and w in _COUNT_BEFORE and digits <= 3) and not loose:
            return True
    two = re.search(r"([^\W\d_]+)\s+([^\W\d_]+)\.?[\s:=#(]{1,6}$", q[:start])
    if two and fold(two.group(1)) in _CODE_WORDS and fold(two.group(2)) in _CODE_WORD_SUFFIX:
        return True
    # after: a space, then the code word ('1861 error', 'the 0x745 error', '1 error(s)')
    after = re.match(r"[ \t]{1,3}([^\W\d_]+)(\(s\))?", q[end:])
    if after is None or fold(after.group(1)) not in _CODE_WORDS:
        return False
    rest = q[end + after.end():]
    # 'Task 1 error 1861', 'Line 34 error C0046': the code word names the code after it
    if re.match(r"[\s:=#('\"‘“]{0,6}(?:0[xX][0-9a-fA-F]|16#[0-9a-fA-F]|#[xX][0-9a-fA-F]|-?\d|[A-Za-z]\d{3,})", rest):
        return False
    if not dec:
        return True                                   # a hex number is no count and no thing's number
    if digits < 2:
        return False                                  # '1 error', 'retry 3 error', 'Step 3 of 5 error': a count
    if _things_number(q, start, end):
        return False                                  # 'Term 4 error', 'Port 851 error', 'line 1861 error'
    w = fold(after.group(1))
    if after.group(2) or w in _COUNT_WORDS:
        listed = re.search(r"(?:0[xX][0-9a-fA-F]+|\d+)" + _LISTED.pattern.replace(r"\Z", "$"), q[:start], re.I)
        if not listed and not (w in _SAME_PLURAL and not after.group(2) and digits >= 4):
            return False                              # '3 errors', '1861 errors' — not '1861 and 1808 errors'
    if re.match(r"[ \t]+([^\W\d_]+)", rest) and fold(re.match(r"[ \t]+([^\W\d_]+)", rest).group(1)) in _COUNT_NOUNS:
        return False                                  # '12 error messages', '10 error codes': how many
    return True


def _cell_value(cell: str) -> tuple[str, int] | None:
    c = cell.strip().strip("`*").strip()
    m = _HEX.match(c)
    if m:
        try:
            return "hex", int(m.group(1).replace("_", ""), 16)
        except ValueError:
            return None
    if re.fullmatch(r"-?\d+", c):                     # a negative decimal too (LabVIEW's -6908)
        return "dec", int(c)
    return None


def definition_score(markdown: str | None, value: int) -> int:
    """How a page holds the code: 2 = a table row with the code as hex and as decimal in cells of their own (a code
    table: '| 0x745 | 1861 | … | ADSERR_CLIENT_SYNCTIMEOUT |'), 1 = a table cell that is the code, 0 = a mention."""
    best = 0
    for ln in (markdown or "").splitlines():
        if not ln.startswith("|"):
            continue
        # third review of 0.8.16: in 32 bits — '| 0xFFFFE504 | -6908 |' is the row of -6908 and of 0xFFFFE504
        forms = {f for f, v in filter(None, (_cell_value(c) for c in ln.strip().strip("|").split("|")))
                 if v & 0xFFFFFFFF == value & 0xFFFFFFFF}
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
