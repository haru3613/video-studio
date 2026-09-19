#!/usr/bin/env python3
"""Automatic TTS pronunciation QA.

We own the script (ground truth), so pronunciation can be verified in a closed
loop: TTS audio -> STT -> align against the script IN PINYIN SPACE -> any
syllable mismatch is a mispronunciation candidate.

Comparing pinyin (not characters) makes the check immune to:
  - traditional/simplified differences (STT returns simplified)
  - homophone spelling variance in STT (治/制 both zhi4 -> no alarm)
while catching real phoneme errors (子 zi vs 洗 xi -> alarm).

Usage:
  verify_pronunciation.py --audio narration.mp3 --text-file narration.txt
  verify_pronunciation.py --audio seg.mp3 --text "這段的逐字稿"

Exit 0 = clean. Exit 1 = mismatches found, OR the transcript covered too little
of the script for the comparison to mean anything (gate-friendly).
Output groups repeated patterns and suggests same-pinyin respelling
candidates. Voice-specific acceptance data belongs to the user's project.
"""

import argparse
import difflib
import json
import mimetypes
import os
import re
import sys
import urllib.request
import uuid

# pypinyin is MANDATORY: without it the gate cannot judge pronunciation, and a
# silently-skipped gate is how mispronunciations shipped (e.g. 救命繩→紙). Fail
# CLOSED with an actionable message + a distinct exit code (2) so callers block
# instead of proceeding. Install for EVERY python3 on PATH the agent might pick
# (caught 2026-06-15: /opt/homebrew/bin/python3 lacked pypinyin → tool crashed →
# "the verifier never really runs").
try:
    from pypinyin import lazy_pinyin, pinyin as _pinyin_het, Style
except ImportError as _e:  # pragma: no cover
    sys.stderr.write(
        f"ERROR: pronunciation gate cannot run — missing dependency: {_e}\n"
        f"  fix: '{sys.executable}' -m pip install pypinyin\n"
        f"  (HARD GATE; exit 2 = could-not-verify, treat as BLOCK not pass)\n")
    sys.exit(2)

_READINGS_CACHE: dict = {}


def valid_readings(ch: str) -> set:
    """All valid base (tone-stripped) pinyin readings of a character.

    A heard syllable that is ANY valid reading of the expected char is NOT a
    mispronunciation — this kills the polyphone/variant false positives that
    drowned the gate (Taiwan-trad 著 reads zhe as a particle but pypinyin's
    default single reading is zhù; STT's 着 reads zhe → both are valid readings
    of 著, so no alarm). Real errors survive: 繩 only reads {sheng}, so hearing
    紙 zhi is a genuine mismatch."""
    if ch not in _READINGS_CACHE:
        got = _pinyin_het(ch, style=Style.TONE3, heteronym=True)
        reads = got[0] if got else []
        _READINGS_CACHE[ch] = {re.sub(r"\d", "", r) for r in reads}
    return _READINGS_CACHE[ch]

STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Coverage gate: the fraction of the script's non-numeral syllables that
# actually ALIGNED to the transcript. Below this the audio does not contain the
# script, so the mismatch count is meaningless — a human reading "18 mismatches"
# would wrongly conclude the audio was checked.
#
# Born 2026-08-05: a take whose TTS silently stopped at 43% of the script
# reported 18 mismatches and 9 warnings, and read as a completed check. The
# untranscribed remainder was one of those warnings. Coverage measured 41.5%.
#
# It compares text, not audio, so it catches short audio AND full-length audio
# that says the wrong thing. It does NOT assume the STT is lossy: that file
# transcribed identically whole and in two halves, so ElevenLabs STT was
# returning everything that was actually spoken — the audio was simply short.
MIN_COVERAGE = 0.90

CJK_RE = re.compile(r"[一-鿿㐀-䶿]")

# STT can make semantic substitutions even when the spoken audio is usable.
# Keep these visible as warnings, but do not hard-fail repeated-pattern gates.
#
# Born from the e-cigarette video: even when TTS input is respelled as 乏 環,
# ElevenLabs STT repeatedly writes the legal term 罰鍰 as 罰款 in context.
ACCEPTED_STT_LEXICAL_SUBS = set()

# Narrow one-off STT substitutions confirmed during full-video gates. Keep these
# context-bound instead of broadening ACCEPTED_STT_LEXICAL_SUBS and hiding real
# future pronunciation errors.
ACCEPTED_STT_CONTEXT_SUBS = set()

# Common chars used when suggesting same-sound respellings (keeps suggestions
# readable instead of obscure dictionary chars).
COMMON = (
    "的一是不了人我在有他這中大來上國個到說們為子和你地出道也時年得就那要下"
    "以生會自著去之過家學對可她裡後小麼心多天而能好都然沒日於起還發成事只作當"
    "想看文無開手十用主行方又如前所本見經頭面公同三已老從動兩長知民樣現分將外"
    "但身些與高意進把法此實回二理美點月明其種聲全工己話兒者向情部正名定女問力"
    "機給等幾很業最間新什打便位因重被走電四第門相次東政海口使教西再平真聽世氣"
    "信北少關並內加化由卻代軍產入先山五太水萬市眼體別處總才場師書比住員九笑性"
    "通目華報立馬命張活難神數件安表原車白應路期叫死常提感金何更反題必都局照運"
    "字紫資姿滋洗喜希西細係戲煙菸言研顏鹽眼演驗厭燕宴艷焰雁硯堰"
)


def load_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    path = os.getenv("ELEVENLABS_API_KEY_PATH")
    if key and path:
        sys.exit("ERROR: configure only one of ELEVENLABS_API_KEY or ELEVENLABS_API_KEY_PATH")
    if path:
        try:
            key = open(os.path.expanduser(path), encoding="utf-8").read().strip()
        except OSError as error:
            sys.exit(f"ERROR: cannot read ELEVENLABS_API_KEY_PATH: {error}")
    if not key:
        sys.exit("ERROR: set ELEVENLABS_API_KEY or ELEVENLABS_API_KEY_PATH")
    return key


# STT writes CJK numerals as Arabic digits (「二零二六年」 -> 「2026年」), and
# cjk_chars() drops Arabic digits entirely — so numerals deflate coverage no
# matter how good the audio is. Measured on the prepay-card-fraud script: 270
# of 3609 CJK chars are numerals, 7.5%, enough to push a clean take under a
# 90% gate. Excluding them from BOTH sides is exact; transliterating between
# the two forms would only be approximate (2026 is 4 chars, 二零二六 is 4 but
# 二二五 is 3).
CJK_NUMERALS = set("〇零一二三四五六七八九十百千萬万億亿兩两")


def coverage_chars(text: str) -> list:
    """CJK chars that STT renders in a stable form — numerals excluded."""
    return [c for c in cjk_chars(text) if c not in CJK_NUMERALS]


def coverage(script_cjk: int, heard_cjk: int) -> float:
    """Heard/script char ratio. 1.0 when script is empty (nothing to miss)."""
    if script_cjk <= 0:
        return 1.0
    return heard_cjk / script_cjk


def stt(audio_path: str, lang: str) -> str:
    return stt_response(audio_path, lang).get("text", "")


def stt_response(audio_path: str, lang: str) -> dict:
    """The whole scribe_v1 response, not just its text.

    The response also carries `words` (per-character start/end/logprob for
    Chinese) and `audio_duration_secs`. This gate only needs the text, but that
    timing data is measured against the audio itself, which makes it the only
    trustworthy source for an SRT -- see stt_align.py. It is already paid for
    in this same call, so it is returned rather than dropped.
    """
    key = load_key()
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(audio_path)[0] or "audio/mpeg"
    with open(audio_path, "rb") as f:
        audio = f.read()

    parts = []
    def field(name, value):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    field("model_id", "scribe_v1")
    field("language_code", lang)
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
         f"filename=\"{os.path.basename(audio_path)}\"\r\n"
         f"Content-Type: {ctype}\r\n\r\n").encode() + audio + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        STT_URL, data=body, method="POST",
        headers={"xi-api-key": key,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=600))
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR STT HTTP {e.code}: {e.read().decode()[:400]}")
    return d


def cjk_chars(text: str) -> list:
    text = text.replace("没", "沒")
    return CJK_RE.findall(text)


def syllables(chars: list) -> list:
    """Base pinyin syllables (tones stripped — neutral-tone variance is noise)."""
    toned = lazy_pinyin("".join(chars), style=Style.TONE3)
    return [re.sub(r"\d", "", s) for s in toned]


def suggest_respellings(ch: str, max_n: int = 5) -> list:
    """Common chars sharing the char's exact toned pinyin (for a voice pronunciation rule)."""
    target = lazy_pinyin(ch, style=Style.TONE3)[0]
    out = []
    for c in COMMON:
        if c == ch or c in out:
            continue
        if lazy_pinyin(c, style=Style.TONE3)[0] == target:
            out.append(c)
        if len(out) >= max_n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--text-file")
    ap.add_argument("--lang", default="zho")
    ap.add_argument("--json-out", help="write machine-readable report here")
    args = ap.parse_args()

    truth = args.text if args.text else open(args.text_file, encoding="utf-8").read()
    heard_text = stt(args.audio, args.lang)

    t_chars = cjk_chars(truth)
    h_chars = cjk_chars(heard_text)
    # Coverage counts syllables that actually ALIGNED, not just how many came
    # back. A pure count ratio can be gamed by a swallowed stretch (a delete
    # block) cancelling out against an ASR loop elsewhere (an insert block) —
    # both are non-blocking warnings, so the ratio lands near 1.0 with nothing
    # hard-failing. That is the same shape as the incident this gate exists to
    # stop. Matching blocks also bound the value at 1.0; a count ratio could
    # report "coverage: 153%" when the transcript hallucinated filler.
    #
    # Measured on the numeral-free subset (see coverage_chars); the mismatch
    # diff below still runs over every CJK char.
    t_cov_chars = coverage_chars(truth)
    h_cov_chars = coverage_chars(heard_text)
    cov_matcher = difflib.SequenceMatcher(
        a=syllables(t_cov_chars), b=syllables(h_cov_chars), autojunk=False)
    cov_matched = sum(block.size for block in cov_matcher.get_matching_blocks())
    cov = coverage(len(t_cov_chars), cov_matched)
    t_syl = syllables(t_chars)
    h_syl = syllables(h_chars)

    sm = difflib.SequenceMatcher(a=t_syl, b=h_syl, autojunk=False)
    mismatches, warnings = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        # Heteronym/variant filter: a char-aligned replace where every heard
        # syllable is a VALID reading of the expected char is not an error
        # (kills 著/着, 調/调 polyphone false positives; keeps 繩→紙).
        if op == "replace" and (i2 - i1) == (j2 - j1):
            if all(h_syl[j1 + k] in valid_readings(t_chars[i1 + k]) for k in range(i2 - i1)):
                continue
        exp = "".join(t_chars[i1:i2])
        got = "".join(h_chars[j1:j2])
        ctx = "".join(t_chars[max(0, i1 - 4):min(len(t_chars), i2 + 4)])
        rec = {"op": op, "expected": exp, "heard": got, "context": ctx,
               "expected_pinyin": t_syl[i1:i2], "heard_pinyin": h_syl[j1:j2]}
        # replaces = hard signal; insert/delete may be STT number formatting
        (mismatches if op == "replace" else warnings).append(rec)

    # group repeated replace patterns
    groups = {}
    accepted = []
    hard_mismatches = []
    for m in mismatches:
        if (m["expected"], m["heard"]) in ACCEPTED_STT_LEXICAL_SUBS or (
            m["expected"],
            m["heard"],
            m["context"],
        ) in ACCEPTED_STT_CONTEXT_SUBS:
            accepted.append(m)
        else:
            hard_mismatches.append(m)
            groups.setdefault((m["expected"], m["heard"]), []).append(m)

    repeated_groups = {k: v for k, v in groups.items() if len(v) > 1}
    coverage_failed = cov < MIN_COVERAGE

    method = {
        "asr_model": "scribe_v1",
        "technique": ("ElevenLabs speech-to-text round trip; transcript diffed "
                      "against the script in pinyin space (pypinyin)"),
        "tone_limitation": ("pinyin comparison strips tone marks (neutral-tone "
                             "variance is noise), so this reliably catches wrong "
                             "initials/finals (子 vs 洗) but is UNRELIABLE for "
                             "tone-only errors (媽 vs 罵) — a clean report does "
                             "not guarantee tones are correct"),
        "coverage": {"value": cov, "threshold": MIN_COVERAGE, "passed": not coverage_failed,
                     "script_chars": len(t_cov_chars), "heard_chars": len(h_cov_chars),
                     "matched_chars": cov_matched,
                     "script_numerals_excluded": len(t_chars) - len(t_cov_chars),
                     "meaning": ("fraction of the script's non-numeral syllables that "
                                 "aligned to the transcript; below threshold the audio "
                                 "does not contain the script and the mismatch list "
                                 "is not a complete check. Numerals are excluded from "
                                 "both sides because STT writes them as Arabic digits")},
        "mismatch_counts": {
            "hard": len(hard_mismatches),
            "repeated_patterns": len(repeated_groups),
            "singletons": len(hard_mismatches) - sum(len(v) for v in repeated_groups.values()),
        },
        "warnings_count": len(warnings),
        "accepted_substitutions_fired": len(accepted),
    }

    print("VERIFICATION METHOD: ElevenLabs scribe_v1 STT round trip, diffed "
          "against script in pinyin syllables.")
    print("  LIMITATION: tone-only errors (e.g. 媽/馬/罵/嗎) are not reliably "
          "caught — only wrong initials/finals are.")
    print("  the whole audio file is sent in one STT request; completeness is "
          "established by the coverage number below, not assumed.")
    print(f"  accepted-substitution suppressions fired: {len(accepted)}")

    print(f"ground truth: {len(t_chars)} CJK chars | STT heard: {len(h_chars)}")
    print(f"coverage: {cov:.1%} (threshold {MIN_COVERAGE:.0%}) measured over "
          f"{len(t_cov_chars)} non-numeral script chars, {cov_matched} aligned "
          f"(transcript had {len(h_cov_chars)}); "
          f"{len(t_chars) - len(t_cov_chars)} numeral chars excluded")
    if coverage_failed:
        print(f"\nCOVERAGE GATE FAILED: only {cov:.1%} of the script's "
              f"{len(t_cov_chars)} non-numeral chars aligned to the STT "
              f"transcript ({cov_matched} matched) — below the {MIN_COVERAGE:.0%} minimum. "
              f"The audio does not contain the script: it is either short, or it "
              f"says something else. Either way the mismatch list below covers "
              f"only the part that was there, so it is NOT a complete check.")
    # GATE FAILS on ANY real (trad/simp-normalised) replace mismatch — repeated OR
    # single. The old "repeated-only" rule shipped 救命繩→紙 because 繩 occurs once;
    # a word that appears once can still be mispronounced. STT noise is suppressed
    # by (a) simp->trad normalisation above and (b) the ACCEPTED_STT_LEXICAL_SUBS
    # allow-list — add a confirmed false positive there, do NOT relax this gate.
    if not hard_mismatches and not coverage_failed:
        print("PRONUNCIATION OK — no syllable mismatches after trad/simp normalisation.")
    elif not hard_mismatches:
        # Coverage failed: "no mismatches" here means "nothing was compared",
        # not "nothing was wrong". render_and_verify.sh pipes this stdout to a
        # log a human greps — printing PRONUNCIATION OK under a failed coverage
        # gate would recreate the exact misreading this gate exists to stop.
        print("NOT CHECKED — coverage gate failed above; no conclusion can be "
              "drawn about pronunciation.")
    else:
        if repeated_groups:
            print(f"\n{sum(len(v) for v in repeated_groups.values())} REPEATED mismatches in {len(repeated_groups)} patterns (high confidence):\n")
            for (exp, got), recs in sorted(repeated_groups.items(), key=lambda kv: -len(kv[1])):
                ep = "/".join(recs[0]["expected_pinyin"])
                gp = "/".join(recs[0]["heard_pinyin"])
                print(f"  「{exp}」({ep}) heard as 「{got}」({gp}) ×{len(recs)}")
                print(f"    e.g. …{recs[0]['context']}…")
                for ch in exp:
                    sug = suggest_respellings(ch)
                    print(f"    respelling candidates for 「{ch}」: {' '.join(sug) if sug else '(none — no common homophone; REWORD the script)'}")
        singletons = [m for m in hard_mismatches if len(groups[(m["expected"], m["heard"])]) == 1]
        if singletons:
            print(f"\n{len(singletons)} SINGLE mismatches (gate FAILS — review each; if confirmed STT noise add to ACCEPTED_STT_LEXICAL_SUBS):")
            for m in singletons:
                ep = "/".join(m["expected_pinyin"])
                gp = "/".join(m["heard_pinyin"])
                sug = suggest_respellings(m["expected"][0]) if m["expected"] else []
                tip = (" " + " ".join(sug)) if sug else " (none — REWORD)"
                print(f"  「{m['expected']}」({ep}) heard as 「{m['heard']}」({gp}) …{m['context']}…  respell:{tip}")
    if accepted:
        print(f"\n{len(accepted)} accepted STT lexical substitutions (reviewed):")
        for m in accepted[:10]:
            ep = "/".join(m["expected_pinyin"])
            gp = "/".join(m["heard_pinyin"])
            print(f"  「{m['expected']}」({ep}) heard as 「{m['heard']}」({gp}) …{m['context']}…")
    if warnings:
        print(f"\n{len(warnings)} insert/delete warnings (possible swallowed "
              f"chars OR STT number/format variance — review manually):")
        for w in warnings[:10]:
            print(f"  [{w['op']}] expected「{w['expected']}」 heard「{w['heard']}」 …{w['context']}…")

    verdict = "FAIL" if (hard_mismatches or coverage_failed) else "PASS"
    print(f"\nVERDICT: {verdict} "
          f"(coverage {'FAIL' if coverage_failed else 'ok'}, "
          f"{len(hard_mismatches)} hard mismatch(es))")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"verdict": verdict, "method": method,
                       "mismatches": mismatches, "warnings": warnings,
                       "heard_text": heard_text,
                       "coverage": cov, "script_cjk": len(t_chars),
                       "heard_cjk": len(h_chars)},
                      f, ensure_ascii=False, indent=1)

    if not hard_mismatches and not coverage_failed:
        # PASS stamp: binds this verification to the exact audio bytes. The done-time
        # gate (render_and_verify.sh --pron-stamp-gate-only) checks the sha still
        # matches, so audio regenerated AFTER a render/verify can't ship on a stale
        # PASS (post-render section-regen + ffmpeg re-mux never re-renders, so the
        # render-path gate never re-fires — this stamp is the only thing that catches it).
        import datetime
        import hashlib
        stamp = {"audio": os.path.basename(args.audio),
                 "sha256": hashlib.sha256(open(args.audio, "rb").read()).hexdigest(),
                 "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "heard_cjk_chars": len(h_chars),
                 # Recorded so the stamp proves WHICH gate passed: a stamp
                 # without these keys predates the coverage gate and cannot
                 # vouch that the audio actually contained the script.
                 "coverage": cov,
                 "coverage_threshold": MIN_COVERAGE,
                 # The HVP tts gate reads this key and requires a list; without
                 # it the gate reported "pronunciation warnings must be a list"
                 # and stayed at warn forever, so a PASS stamp could never
                 # actually satisfy the pipeline it was written for. Empty by
                 # construction — this branch only runs when nothing hard failed.
                 "warnings": [],
                 # Non-blocking insert/delete notes, kept for a human to skim.
                 # Deliberately NOT in "warnings": they are dominated by STT
                 # number-format variance and would block the gate on noise.
                 "review_notes": warnings}
        with open(args.audio + ".pron-ok.json", "w", encoding="utf-8") as f:
            json.dump(stamp, f, ensure_ascii=False, indent=1)
        print(f"PASS stamp written: {args.audio}.pron-ok.json")

    sys.exit(1 if (hard_mismatches or coverage_failed) else 0)


if __name__ == "__main__":
    main()
