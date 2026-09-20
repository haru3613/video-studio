#!/usr/bin/env python3
"""zh-TW text normalization for narration scripts (opt-in, see task-2 brief).

normalize_zh(text) -> text: converts numbers/dates/tickers/currency written in
ASCII digits into their spoken zh-TW Chinese-character reading, so eleven_v3
speaks them correctly (it sometimes mis-reads raw digit strings). Pure
stdlib, no deps, no network — safe to call at script-build time.

Scope (v1, evaluation report §6): percentages, decimals, comma-grouped
integers, 萬/億-suffixed numbers, NT$/US$ currency, years (digit-by-digit),
month/day, clock times, leading-zero TW ticker codes (digit-by-digit).
English words/acronyms are LEFT ALONE in v1 (documented) — a digit run
attached to ASCII letters (e.g. "yes123") or in the hard-coded exception
list (e.g. "7-11") is skipped. Inline [emotion] tags are left alone too.

Normalized readings also appear in the SUBTITLES (SRT display text follows
whatever text was actually sent to TTS), unlike the voice pronunciation
respellings (narration/pronunciation/voice/, applied via
generate_narration_with_srt.py) which are reverted back to the original
display text for subtitles — --normalize output is what viewers will read.

Known limitations:
- comma-separated digit enumerations (e.g. "1,2,3") are read as one
  comma-grouped number by rule 9, not three separate digits — reword the
  script if a literal enumeration reading is needed.
- ratio notation (e.g. "16:9") is mis-read: the clock rule requires an exact
  2-digit minute so it does NOT fire on ":9", but the standalone-integer rule
  still converts the left side alone, giving "十六:9" — avoid ratio notation in
  narration text, or space it (e.g. "16 : 9") to dodge both rules.
"""
import re

DIGITS = "零一二三四五六七八九"
UNITS4 = ["", "十", "百", "千"]
GROUPS = ["", "萬", "億", "兆"]

# Emotion/audio tag, e.g. [curious] or [a bit worried]: never spoken as text,
# left untouched by normalize_zh, and shared with generate_narration_with_srt.py
# (which needs the same pattern to strip/skip tags in the TTS-facing pipeline).
TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z _-]{0,30}\]")


def _four(n: int) -> str:
    """0<=n<10000 -> Chinese, no leading 零 handling across groups (caller does)."""
    if n == 0:
        return ""
    out, started_zero = [], False
    for pos in (3, 2, 1, 0):
        d = (n // 10 ** pos) % 10
        if d == 0:
            if out:
                started_zero = True
            continue
        if started_zero:
            out.append("零")
            started_zero = False
        if pos == 1 and d == 1 and not out:
            out.append("十")          # 10-19: 十X not 一十X
        else:
            out.append(DIGITS[d] + UNITS4[pos])
    return "".join(out)


def num2zh(n: int) -> str:
    if n == 0:
        return "零"
    parts = []
    gi = 0
    while n > 0:
        n, q = divmod(n, 10000)
        parts.append((q, GROUPS[gi], gi))
        gi += 1
    parts.reverse()
    out = []
    prev_gi = None  # significance index (0=units,1=萬,2=億,3=兆) of the last EMITTED group
    for q, g, gi in parts:
        if q == 0:
            continue  # fully-zero group: skip, but do NOT advance prev_gi
        # inter-group 零 bridge needed when either:
        #  (a) this group's own leading digit is zero (q < 1000), e.g. 一億零五萬
        #  (b) at least one whole group between here and the previous emitted
        #      group was fully zero and thus skipped above (prev_gi - gi > 1),
        #      e.g. 一億....一千 skips an all-zero 萬 group -> 一億零一千
        if out and (q < 1000 or (prev_gi is not None and prev_gi - gi > 1)):
            out.append("零")
        out.append(_four(q) + g)
        prev_gi = gi
    return "".join(out)


def digits2zh(s: str) -> str:
    """Digit-by-digit reading: '0050' -> '零零五零', '2026' -> '二零二六'."""
    return "".join(DIGITS[int(c)] for c in s)


def decimal2zh(int_part: str, frac_part: str) -> str:
    return num2zh(int(int_part)) + "點" + digits2zh(frac_part)


def _num_str_to_zh(num_str: str) -> str:
    """Comma-grouped digit string, optionally with one decimal point, to zh reading."""
    num_str = num_str.replace(",", "")
    if "." in num_str:
        int_part, frac_part = num_str.split(".", 1)
        return decimal2zh(int_part or "0", frac_part)
    return num2zh(int(num_str))


# 二千/二萬/二億 -> 兩千/兩萬/兩億. Scoped to currency readings only (brief rule 2);
# plain digit reads (rule 9) and 萬/億-suffix reads (rule 8) keep 二 as-is.
_TWO_MAP_RE = re.compile("二(?=[千萬億])")


def _apply_two(s: str) -> str:
    return _TWO_MAP_RE.sub("兩", s)


# Tokens normalization must never touch, matched (and stashed) before any numeric
# rule runs: emotion tags (TAG_RE, defined above), the hard-coded 7-11 exception
# list, and any digit run glued to ASCII letters (e.g. yes123 -- English+digits,
# left alone in v1).
# \b is unusable here: CJK ideographs count as \w to Python's re engine, so
# e.g. "0050漲了" has NO word boundary between "0" and "漲" (both sides \w) --
# \b0\d{3,5}\b would silently fail to match a ticker glued to Chinese text.
# Use ASCII-scoped lookaround instead: "not immediately preceded/followed by
# an ASCII letter or digit". A CJK neighbor never blocks the match; only an
# adjacent ASCII alnum (e.g. "abc0050", "0050x") does.
_NOT_BEFORE_ASCII_ALNUM = r"(?<![0-9A-Za-z])"
_NOT_AFTER_ASCII_ALNUM = r"(?![0-9A-Za-z])"

_PROTECT_RE = re.compile(
    TAG_RE.pattern +
    r"|" + _NOT_BEFORE_ASCII_ALNUM + r"7-11" + _NOT_AFTER_ASCII_ALNUM +
    r"|" + _NOT_BEFORE_ASCII_ALNUM + r"7-Eleven" + _NOT_AFTER_ASCII_ALNUM +
    r"|[A-Za-z]+\d[\d,]*(?:\.\d+)?"
)

_NUM = r"[\d,]+(?:\.\d+)?"
_NT_RE = re.compile(r"NT\$\s?(" + _NUM + r")")
_US_RE = re.compile(r"(?:US\$|\$)(" + _NUM + r")")
_PCT_RE = re.compile(r"(" + _NUM + r")%")
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_YEAR_RE = re.compile(r"(\d{4})\s*年")
_MONTH_RE = re.compile(r"(\d{1,2})\s*月")
_DAY_RE = re.compile(r"(\d{1,2})\s*日")
_TICKER_RE = re.compile(_NOT_BEFORE_ASCII_ALNUM + r"0\d{3,5}" + _NOT_AFTER_ASCII_ALNUM)
_WANYI_RE = re.compile(r"(" + _NUM + r")\s*(萬|億)")
_DECIMAL_RE = re.compile(r"[\d,]*\d\.\d+")
_INT_RE = re.compile(r"[\d,]{2,}")

# Stash placeholder: one dedicated Private-Use-Area codepoint per stashed span
# (chr(_PUA_BASE + idx)), no digits/delimiters at all. The prior bare-digit-index
# scheme ("<idx>") got re-tokenized by _INT_RE once idx >= 10 ("10" itself matches
# [\d,]{2,}), corrupting the placeholder before restore. A lone PUA codepoint is
# never \d, so no numeric rule above can ever re-tokenize it.
# Assumption: input narration text contains no PUA (U+E000-U+F8FF) characters
# (true for this pipeline) -- the guard in normalize_zh() below raises loudly if
# that assumption is ever violated, instead of silently leaking a PUA char.
_PUA_BASE = 0xE000
_PUA_MAX = 0xF8FF
_PUA_RE = re.compile(f"[{chr(_PUA_BASE)}-{chr(_PUA_MAX)}]")


def _clock_repl(m: "re.Match") -> str:
    hour, minute = m.group(1), m.group(2)
    minute_read = digits2zh(minute) if minute.startswith("0") else num2zh(int(minute))
    return f"{num2zh(int(hour))}點{minute_read}分"


def _int_repl(m: "re.Match") -> str:
    s = m.group(0)
    if not any(c.isdigit() for c in s):
        return s  # pure-comma leftover (shouldn't happen in practice); leave untouched
    return _num_str_to_zh(s)


def normalize_zh(text: str) -> str:
    """Pure function, idempotent, no deps. See module docstring for scope."""
    # Input containing PUA codepoints would alias stash placeholders and get
    # silently substituted — refuse up-front instead (narration text never
    # legitimately contains PUA characters).
    if _PUA_RE.search(text):
        raise ValueError("zh_normalize: input contains private-use-area characters")
    protected = []

    def _stash(m: "re.Match") -> str:
        if len(protected) > _PUA_MAX - _PUA_BASE:
            raise ValueError("zh_normalize: too many protected spans (>6400)")
        protected.append(m.group(0))
        return chr(_PUA_BASE + len(protected) - 1)

    s = _PROTECT_RE.sub(_stash, text)

    s = _NT_RE.sub(lambda m: f"新台幣{_apply_two(_num_str_to_zh(m.group(1)))}元", s)
    s = _US_RE.sub(lambda m: f"{_apply_two(_num_str_to_zh(m.group(1)))}美元", s)
    s = _PCT_RE.sub(lambda m: f"百分之{_num_str_to_zh(m.group(1))}", s)
    s = _CLOCK_RE.sub(_clock_repl, s)
    s = _YEAR_RE.sub(lambda m: f"{digits2zh(m.group(1))}年", s)
    s = _MONTH_RE.sub(lambda m: f"{num2zh(int(m.group(1)))}月", s)
    s = _DAY_RE.sub(lambda m: f"{num2zh(int(m.group(1)))}日", s)
    s = _TICKER_RE.sub(lambda m: digits2zh(m.group(0)), s)
    s = _WANYI_RE.sub(lambda m: f"{_num_str_to_zh(m.group(1))}{m.group(2)}", s)
    s = _DECIMAL_RE.sub(lambda m: _num_str_to_zh(m.group(0)), s)
    s = _INT_RE.sub(_int_repl, s)

    def _restore(m: "re.Match") -> str:
        idx = ord(m.group(0)) - _PUA_BASE
        if idx < len(protected):
            return protected[idx]
        return m.group(0)  # not one of ours -- leave for the guard below to catch

    result = _PUA_RE.sub(_restore, s)

    # Loud guard: silent corruption is never acceptable in narration text. Any
    # leftover PUA codepoint here means either a stash placeholder failed to
    # restore, or the "no PUA in input" assumption above was violated.
    if _PUA_RE.search(result):
        raise ValueError("zh_normalize: unresolved protected-span placeholder")

    return result
