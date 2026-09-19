#!/usr/bin/env python3
"""Separate real TTS mispronunciations from STT noise, without a human ear.

The gate (`verify_pronunciation.py`) diffs an STT transcript against the script
and flags disagreements. Some are real — the voice genuinely says the wrong
thing — and some are the transcriber's fault. Telling them apart by reasoning
does not work; it was tried on a 13-minute narration and got both directions
wrong, first dismissing every flag as noise, then distrusting the detector
wholesale. Both conclusions were argued from evidence and both were false.

What does work is an experiment. Put the suspect term in a short carrier
sentence, generate it on its own, and transcribe that:

  * the error reproduces in isolation  -> the voice really mispronounces it
  * the term comes back clean          -> the flag was noise in long-form context

Measured on one narration: 毆 / 傳 / 擬 / 欺 / 櫃 / 慎 reproduced and were real;
違反 / 業界 / 廣告主 / 賠償 came back clean and were noise. Roughly ten seconds
of audio and a few dozen credits decided ten flags that reasoning got wrong.

The same loop then verifies a fix. Same-sound respellings are proposed from
pypinyin, generated, and re-transcribed; whatever now reads correctly is a
working entry for the voice's file in narration/pronunciation/voice/. A term
where no candidate survives needs the sentence reworded, and saying so is
more useful than a respelling nobody checked.

Terms are batched into one clip. Ten of them cost about a hundred credits
together, which is what makes this affordable enough to run every video.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verify_pronunciation as vp  # noqa: E402

GENERATE = HERE / "generate_narration_with_srt.py"

# Plain frames that add no unusual vocabulary of their own — the term under test
# should be the only thing that can go wrong in the sentence.
CARRIERS = [
    "這裡要說的是{}這個詞。",
    "我們接下來談{}。",
    "報告裡提到了{}。",
]


def probe_text(terms):
    return "".join(CARRIERS[i % len(CARRIERS)].format(t) for i, t in enumerate(terms))


def synthesise(
    text, out_base, model, voice=None, extra_env=None, force_budget=False
):
    out_base.parent.mkdir(parents=True, exist_ok=True)
    src = out_base.with_suffix(".txt")
    src.write_text(text, encoding="utf-8")
    env = {**os.environ, **(extra_env or {})}
    command = [sys.executable, str(GENERATE), "--text-file", str(src),
               "--out-base", str(out_base), "--model", model]
    if voice:
        command.extend(["--voice", voice])
    if force_budget:
        command.append("--force-budget")
    result = subprocess.run(
        command,
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        sys.exit(f"ERROR: synthesis failed\n{result.stdout}\n{result.stderr}")
    credits = re.search(r"credits=(\d+)", result.stdout)
    return int(credits.group(1)) if credits else 0


def mismatched_chars(audio: Path, text: Path) -> set:
    """Characters the gate flags in this clip, via the gate's own diff."""
    report = audio.with_suffix(".probe.json")
    subprocess.run(
        [sys.executable, str(HERE / "verify_pronunciation.py"),
         "--audio", str(audio), "--text-file", str(text),
         "--json-out", str(report)],
        capture_output=True, text=True,
    )
    if not report.exists():
        return set()
    data = json.loads(report.read_text(encoding="utf-8"))
    flagged = set()
    for m in data.get("mismatches", []):
        flagged.update(vp.cjk_chars(m.get("expected") or ""))
    return flagged


def same_syllable_chars(ch: str, limit: int) -> list:
    """Common characters sharing this character's syllable, tone included or not.

    `vp.suggest_respellings` requires an exact tone match, which is stricter
    than what actually works: 詐欺 is fixed by writing 詐期 even though 欺 is
    qī and 期 is qí. Tone-exact candidates come first, then same-syllable ones
    — the audio check downstream discards whatever does not read correctly, so
    a wider pool costs nothing but a few characters of synthesis.
    """
    exact = [c for c in vp.suggest_respellings(ch, max_n=limit) if c != ch]
    target = re.sub(r"\d", "", vp.lazy_pinyin(ch, style=vp.Style.TONE3)[0])
    loose = []
    for cand in vp.COMMON:
        if cand == ch or cand in exact:
            continue
        if re.sub(r"\d", "", vp.lazy_pinyin(cand, style=vp.Style.TONE3)[0]) == target:
            loose.append(cand)
        if len(loose) >= limit:
            break
    return exact + loose


def candidates_for(term: str, limit: int) -> list:
    """Same-sound respellings of `term`, one character substituted at a time.

    Only characters the gate actually flags are worth substituting, but at this
    point we do not know which, so vary each in turn and let the audio decide.
    """
    out = []
    for i, ch in enumerate(term):
        for alt in same_syllable_chars(ch, limit):
            out.append(term[:i] + alt + term[i + 1:])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", action="append", default=[],
                    help="term to test; repeat. Or use --report.")
    ap.add_argument("--report", help="gate JSON report to pull flagged terms from")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--model", default="eleven_v3")
    ap.add_argument("--voice")
    ap.add_argument("--max-candidates", type=int, default=3,
                    help="respellings tried per character")
    ap.add_argument("--candidate", action="append", default=[], metavar="TERM=RESPELL",
                    help="propose a respelling the built-in table cannot reach, e.g. "
                         "毆打=歐打. vp.COMMON is a hand-kept list and does not "
                         "contain 歐 or 貴, so working fixes exist outside it. "
                         "Injected candidates are verified by audio like any other.")
    ap.add_argument("--passes", type=int, default=3,
                    help="isolation probes per run. TTS and STT both vary between "
                         "calls, so a single probe disagrees with itself; a term "
                         "counts as real only if it reproduces in a majority.")
    ap.add_argument("--no-fix", action="store_true",
                    help="only classify real vs noise; do not look for respellings")
    ap.add_argument("--ab", action="store_true",
                    help="always generate original A and injected-candidate B clips")
    ap.add_argument("--force-budget", action="store_true",
                    help="use the caller's explicit spending approval")
    args = ap.parse_args()

    injected = {}
    for item in args.candidate:
        term, _, respell = item.partition("=")
        if not respell:
            sys.exit(f"ERROR: --candidate needs TERM=RESPELL, got {item!r}")
        injected.setdefault(term, []).append(respell)

    terms = list(dict.fromkeys(args.term))
    if args.report:
        data = json.loads(Path(args.report).read_text(encoding="utf-8"))
        for m in data.get("mismatches", []):
            if (t := (m.get("expected") or "").strip()):
                terms.append(t)
        terms = list(dict.fromkeys(terms))
    if not terms:
        sys.exit("ERROR: no terms given")

    out = Path(args.out_dir)
    spent = 0
    budget_env = {"TTS_BUDGET_OK": time.strftime("%Y-%m-%d")} if args.force_budget else None
    if args.ab:
        if set(injected) != set(terms) or any(len(values) != 1 for values in injected.values()):
            sys.exit("ERROR: --ab requires exactly one --candidate for every term")
        a_base = out / "probe-a-original"
        b_base = out / "probe-b-g2p"
        fixes = {term: injected[term][0] for term in terms}
        spent += synthesise(
            probe_text(terms), a_base, args.model, args.voice, budget_env, args.force_budget
        )
        spent += synthesise(
            probe_text([fixes[term] for term in terms]),
            b_base,
            args.model,
            args.voice,
            budget_env,
            args.force_budget,
        )
        audio = {}
        for label, base in (("a", a_base), ("b", b_base)):
            path = base.with_suffix(".mp3")
            audio[label] = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        verdict = {
            "schema": "haru.pronunciation_confirmation.v1",
            "mode": "ab",
            "terms": terms,
            "fixes": fixes,
            "voice": args.voice,
            "model": args.model,
            "credits_spent": spent,
            "audio": audio,
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "confirmation.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"credits spent: {spent} | wrote {out / 'confirmation.json'}")
        return 0

    print(f"probing {len(terms)} terms in isolation, {args.passes} passes…")
    hits = {t: 0 for t in terms}
    for p in range(args.passes):
        base = out / f"probe-terms-{p + 1}"
        spent += synthesise(
            probe_text(terms), base, args.model, args.voice, budget_env, args.force_budget
        )
        flagged = mismatched_chars(base.with_suffix(".mp3"), base.with_suffix(".txt"))
        for t in terms:
            if set(vp.cjk_chars(t)) & flagged:
                hits[t] += 1
        print(f"  pass {p + 1}: {' '.join(t for t in terms if set(vp.cjk_chars(t)) & flagged) or '無'}")

    # Majority, not any-hit: one pass flagging a term is within the noise these
    # two stochastic systems produce on their own.
    threshold = args.passes // 2 + 1
    real = [t for t in terms if hits[t] >= threshold]
    noise = [t for t in terms if t not in real]
    print(f"\n  出現次數: " + "  ".join(f"{t}={hits[t]}/{args.passes}" for t in terms))

    print(f"\n  真錯（孤立情境重現）: {' '.join(real) or '無'}")
    print(f"  誤報（孤立情境正常）: {' '.join(noise) or '無'}")

    fixes, unfixable = {}, []
    if real and not args.no_fix:
        pool = []
        for t in real:
            # Injected candidates go first, so a human-proposed fix wins over a
            # generated one when both read correctly.
            pool += [(t, c) for c in injected.get(t, [])]
            pool += [(t, c) for c in candidates_for(t, args.max_candidates)]
        if pool:
            print(f"\ntrying {len(pool)} respellings…")
            cand = out / "probe-respell"
            spent += synthesise(
                probe_text([c for _, c in pool]), cand, args.model, args.voice,
                budget_env, args.force_budget,
            )
            bad = mismatched_chars(cand.with_suffix(".mp3"), cand.with_suffix(".txt"))
            for term, candidate in pool:
                if term in fixes:
                    continue
                if not (set(vp.cjk_chars(candidate)) & bad):
                    fixes[term] = candidate
        unfixable = [t for t in real if t not in fixes]

    print("\n=== 判定 ===")
    for t in real:
        if t in fixes:
            print(f"  {t}  →  改寫為 {fixes[t]}（已驗證讀對）")
        else:
            print(f"  {t}  →  無可用同音字，需改寫句子")
    for t in noise:
        print(f"  {t}  →  STT 誤報，音檔無誤")

    verdict = {
        "schema": "haru.pronunciation_confirmation.v1",
        "terms": terms,
        "real": real,
        "noise": noise,
        "fixes": fixes,
        "needs_rewording": unfixable,
        "credits_spent": spent,
    }
    (out / "confirmation.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\ncredits spent: {spent} | wrote {out / 'confirmation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
