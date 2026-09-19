import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("pronunciation_workflow.py")


def test_executable_keeps_virtualenv_symlink_path():
    spec = importlib.util.spec_from_file_location("pronunciation_workflow", SCRIPT)
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    with tempfile.TemporaryDirectory() as tmp:
        python = Path(tmp) / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        old = os.environ.get("HARU_G2PW_PYTHON")
        os.environ["HARU_G2PW_PYTHON"] = str(python)
        try:
            assert workflow.executable_from_env("HARU_G2PW_PYTHON", "python3") == python
        finally:
            if old is None:
                os.environ.pop("HARU_G2PW_PYTHON", None)
            else:
                os.environ["HARU_G2PW_PYTHON"] = old


def test_executable_searches_standard_package_manager_paths(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("pronunciation_workflow", SCRIPT)
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    python = tmp_path / "python3.11"
    python.write_text("", encoding="utf-8")

    def fake_which(name, path=None):
        assert name == "python3.11"
        assert "/opt/homebrew/bin" in path
        assert "/usr/local/bin" in path
        return str(python)

    monkeypatch.delenv("HARU_TTS_PYTHON", raising=False)
    monkeypatch.setattr(workflow.shutil, "which", fake_which)
    assert workflow.executable_from_env("HARU_TTS_PYTHON", "python3.11") == python


def test_pronunciation_provider_failure_is_classified_without_exposing_body():
    spec = importlib.util.spec_from_file_location("pronunciation_workflow", SCRIPT)
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    assert (
        workflow.pronunciation_failure_code(
            'ERROR HTTP 429: {"detail":"provider body must stay private"}'
        )
        == "pronunciation_provider_http_429"
    )


def run(*args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_analyze_promotes_a_source_bound_plan():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "project"
        tools = root / "media-tools"
        (project / ".hvp/staging").mkdir(parents=True)
        (tools / "narration").mkdir(parents=True)
        narration = "銀行行動"
        (project / "narration.txt").write_text(narration, encoding="utf-8")
        fake = tools / "narration/g2p_plan.py"
        fake.write_text(
            "import argparse, hashlib, json\n"
            "p=argparse.ArgumentParser(); p.add_argument('--text-file'); p.add_argument('--out'); p.add_argument('--overrides'); a=p.parse_args()\n"
            "raw=open(a.text_file,'rb').read()\n"
            "plan={'schema':'haru.pronunciation_plan.v1','source':{'sha256':hashlib.sha256(raw).hexdigest()},'engine':{'name':'g2pw'},'overrides_sha256':None,'phonemes':[],'review_items':[],'summary':{}}\n"
            "open(a.out,'w').write(json.dumps(plan))\n",
            encoding="utf-8",
        )

        first = run(
            "analyze",
            project,
            tools,
            env={"HARU_G2PW_PYTHON": sys.executable},
        )
        assert first.returncode == 0, first.stderr
        result = json.loads(first.stdout)
        plan = project / "pronunciation-plan.json"
        assert result["schema"] == "haru.pronunciation_analysis.v1"
        assert result["status"] == "complete"
        assert result["source_sha256"] == hashlib.sha256(narration.encode()).hexdigest()
        assert result["output_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
        assert json.loads(plan.read_text())["schema"] == "haru.pronunciation_plan.v1"


def test_confirm_is_bounded_and_reuses_completed_probe_audio():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "project"
        tools = root / "media-tools"
        (project / ".hvp/staging").mkdir(parents=True)
        (tools / "narration").mkdir(parents=True)
        narration = project / "narration.txt"
        narration.write_text("代送鑑定不能說謊。\n", encoding="utf-8")
        source_sha = hashlib.sha256(narration.read_bytes()).hexdigest()
        plan = project / "pronunciation-plan.json"
        plan.write_text(
            json.dumps(
                {
                    "schema": "haru.pronunciation_plan.v1",
                    "source": {"sha256": source_sha},
                }
            ),
            encoding="utf-8",
        )
        plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
        (project / ".hvp/pronunciation-analysis.json").write_text(
            json.dumps(
                {
                    "schema": "haru.pronunciation_analysis.v1",
                    "project": "project",
                    "status": "complete",
                    "source": "narration.txt",
                    "source_sha256": source_sha,
                    "output": "pronunciation-plan.json",
                    "output_sha256": plan_sha,
                }
            ),
            encoding="utf-8",
        )
        request = project / ".hvp/staging/pronunciation-probe-request.json"
        request.write_text(
            json.dumps(
                {
                    "schema": "haru.pronunciation_probe_request.v1",
                    "terms": ["代送鑑定", "說謊"],
                    "passes": 1,
                    "model": "eleven_v3",
                    "max_credits": 100,
                    "spending_approved_by": "harvey",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        calls = tools / "calls"
        (tools / "narration/confirm_pronunciation.py").write_text(
            "import argparse, json, pathlib\n"
            "p=argparse.ArgumentParser(); p.add_argument('--term',action='append'); p.add_argument('--out-dir'); p.add_argument('--model'); p.add_argument('--passes'); p.add_argument('--no-fix',action='store_true'); a=p.parse_args()\n"
            f"pathlib.Path({str(calls)!r}).open('a').write('1')\n"
            "out=pathlib.Path(a.out_dir); out.mkdir(parents=True,exist_ok=True)\n"
            "(out/'probe-terms-1.mp3').write_bytes(b'probe-audio')\n"
            "(out/'confirmation.json').write_text(json.dumps({'schema':'haru.pronunciation_confirmation.v1','terms':a.term,'real':[],'noise':a.term,'fixes':{},'needs_rewording':[],'credits_spent':42}))\n",
            encoding="utf-8",
        )

        results = []
        for _ in range(2):
            result = run(
                "confirm",
                project,
                tools,
                env={"HARU_TTS_PYTHON": sys.executable},
            )
            assert result.returncode == 0, result.stderr
            results.append(json.loads(result.stdout))

        assert results[0] == results[1]
        assert calls.read_text() == "1"
        receipt = results[0]
        assert receipt["schema"] == "haru.pronunciation_probe.v1"
        assert receipt["plan_sha256"] == plan_sha
        assert receipt["credits_spent"] == 42
        assert receipt["max_credits"] == 100
        assert receipt["audio"][0]["sha256"] == hashlib.sha256(b"probe-audio").hexdigest()

        reviewed = run(
            "review",
            project,
            "--reviewed-by",
            "harvey",
            "--verdict",
            "pass",
            "--notes",
            "listened to both terms",
        )
        assert reviewed.returncode == 0, reviewed.stderr
        review = json.loads(reviewed.stdout)
        assert review["schema"] == "haru.pronunciation_review.v1"
        assert review["verdict"] == "pass"
        assert review["plan_sha256"] == plan_sha
        assert review["probe_sha256"] == hashlib.sha256(
            (project / ".hvp/pronunciation-probes.json").read_bytes()
        ).hexdigest()

        current = run("check", project)
        assert current.returncode == 0, current.stderr
        assert json.loads(current.stdout)["code"] == "pronunciation_review_current"

        confirmation = project / receipt["confirmation"]["path"]
        confirmation_bytes = confirmation.read_bytes()
        confirmation.write_text("{}", encoding="utf-8")
        tampered = run("check", project)
        assert tampered.returncode == 3
        assert json.loads(tampered.stdout)["code"] == "pronunciation_review_stale"
        confirmation.write_bytes(confirmation_bytes)

        narration.write_text("代送鑑定不能說謊。文字改了。", encoding="utf-8")
        stale = run("check", project)
        assert stale.returncode == 3
        assert json.loads(stale.stdout)["code"] == "pronunciation_review_stale"


# --- shared fixtures for the ab-probe -> review -> generate-narration flow ---
#
# confirm_pronunciation.py's --ab path always bakes the approved respelling
# into the literal probe text it sends to TTS (see probe-b-g2p.txt below); it
# has no pronunciation-dictionary option. HVP_TEST_B_MECHANISM lets a test
# simulate an old-shape probe whose b-clip take.json instead carries a
# pronunciation_dictionary locator, to exercise probe_mechanism()'s
# derive-from-disk path and the mismatch gate.

FAKE_G2P_PLAN_SRC = (
    "import argparse, hashlib, json\n"
    "p=argparse.ArgumentParser(); p.add_argument('--text-file'); p.add_argument('--out'); p.add_argument('--overrides'); a=p.parse_args()\n"
    "raw=open(a.text_file,'rb').read(); overrides=json.load(open(a.overrides))\n"
    "good={'說謊':'說恍','代送鑑定':'代送見定'}\n"
    "items=[{'reason':'project_override','term':x['term'],'spoken':x['spoken'],'g2p_match':good.get(x['term'])==x['spoken']} for x in overrides['terms']]\n"
    "plan={'schema':'haru.pronunciation_plan.v1','source':{'sha256':hashlib.sha256(raw).hexdigest()},'review_items':items}\n"
    "open(a.out,'w').write(json.dumps(plan))\n"
)

FAKE_CONFIRM_PRONUNCIATION_SRC = (
    "import argparse, json, os, pathlib\n"
    "p=argparse.ArgumentParser(); p.add_argument('--term',action='append'); p.add_argument('--candidate',action='append'); p.add_argument('--out-dir'); p.add_argument('--model'); p.add_argument('--voice'); p.add_argument('--ab',action='store_true'); p.add_argument('--force-budget',action='store_true'); a=p.parse_args()\n"
    "pathlib.Path(__CALLS__).open('a').write(json.dumps(vars(a),ensure_ascii=False)+'\\n')\n"
    "out=pathlib.Path(a.out_dir); out.mkdir(parents=True,exist_ok=True)\n"
    "pa=out/'probe-a-original.mp3'; pb=out/'probe-b-g2p.mp3'; pa.write_bytes(b'original'); pb.write_bytes(b'g2p')\n"
    "fixes=dict(x.split('=',1) for x in a.candidate)\n"
    "(out/'probe-b-g2p.txt').write_text(''.join(fixes.values()), encoding='utf-8')\n"
    "mechanism=os.environ.get('HVP_TEST_B_MECHANISM','inline_respelling')\n"
    "b_take={'pronunciation_dictionary': {'pronunciation_dictionary_id': 'dict-1'}} if mechanism=='dictionary' else {}\n"
    "(out/'probe-b-g2p.take.json').write_text(json.dumps(b_take))\n"
    "audio={'a':{'path':str(pa),'sha256':'x'},'b':{'path':str(pb),'sha256':'y'}}\n"
    "(out/'confirmation.json').write_text(json.dumps({'schema':'haru.pronunciation_confirmation.v1','mode':'ab','terms':a.term,'fixes':fixes,'voice':a.voice,'model':a.model,'credits_spent':80,'audio':audio}))\n"
)

# The fake sectioned narration generator writes ".dbdb" (mean_volume) and
# ".duration" sidecar files next to each mp3, which the fake ffmpeg/ffprobe
# below read instead of decoding real audio -- both stay fully offline and
# never depend on real audio codecs being installed on the runner.
#
# HVP_FAKE_ALIGNMENT_FAILURE controls what gets corrupted:
#   zero_duration    -- merged SRT gets a 3-cue same-timestamp cluster
#                        (the structural signature of a truncated take)
#   coverage_gap     -- merged SRT's last cue ends far past the audio
#   section_cluster  -- only section-001's own SRT gets the 3-cue cluster,
#                        merged SRT stays clean (proves the per-section loop,
#                        not just the merged check, is load-bearing)
# HVP_FAKE_CACHED=1 reports run_credit_report.actual_credits as null, the
# real shape generate_sectioned_narration.py's credit_report() returns when
# every section was already cached.
FAKE_SECTIONED_GENERATOR_SRC = """
import argparse, hashlib, json, os, pathlib


def to_srt(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def cue(index, start, end, text):
    return "%d\\n%s --> %s\\n%s\\n" % (index, to_srt(start), to_srt(end), text)


p = argparse.ArgumentParser()
p.add_argument('--text-file'); p.add_argument('--out-base'); p.add_argument('--sections-dir')
p.add_argument('--voice'); p.add_argument('--pronunciation-overrides')
p.add_argument('--target-chars'); p.add_argument('--max-chars')
p.add_argument('--force-budget', action='store_true')
a = p.parse_args()

pathlib.Path(__CALLS__).open('a').write(
    '%s %s/%s\\n' % (os.environ.get('TTS_BUDGET_OK', 'missing'), a.target_chars, a.max_chars)
)

source = pathlib.Path(a.text_file)
text = source.read_text(encoding='utf-8').strip()
out_base = pathlib.Path(a.out_base)
sections_dir = pathlib.Path(a.sections_dir)
sections_dir.mkdir(parents=True, exist_ok=True)

db_values = [float(v) for v in os.environ.get('HVP_FAKE_SECTION_DB', '-20').split(',')]
duration_total = float(os.environ.get('HVP_FAKE_DURATION', str(len(text) / 4.0)))
failure = os.environ.get('HVP_FAKE_ALIGNMENT_FAILURE', '')
cached = os.environ.get('HVP_FAKE_CACHED', '') == '1'
n = len(db_values)
bounds = [len(text) * i // n for i in range(n + 1)]
bounds[-1] = len(text)
per_duration = duration_total / n

sections = []
for index, db in enumerate(db_values, start=1):
    start, end = bounds[index - 1], bounds[index]
    section_id = 'section-%03d' % index
    mp3 = sections_dir / (section_id + '.mp3')
    mp3.write_bytes(('audio-%d' % index).encode())
    # The pre-respelling text handed to the provider, as run_generator writes it.
    # HVP_FAKE_OVERSIZED_SECTION models a child that ignored --max-chars.
    (sections_dir / (section_id + '.txt')).write_text(
        text[start:end] + 'x' * int(os.environ.get('HVP_FAKE_OVERSIZED_SECTION', '0')),
        encoding='utf-8',
    )
    (sections_dir / (section_id + '.mp3.dbdb')).write_text(str(db))
    (sections_dir / (section_id + '.mp3.duration')).write_text(str(per_duration))
    srt = sections_dir / (section_id + '.srt')
    if failure == 'section_cluster' and index == 1:
        body = (
            cue(1, 0, per_duration * 0.3, 'text')
            + cue(2, per_duration, per_duration, 'x')
            + cue(3, per_duration, per_duration, 'x')
            + cue(4, per_duration, per_duration, 'x')
        )
    else:
        body = cue(1, 0, per_duration, 'text')
    srt.write_text(body, encoding='utf-8')
    sections.append({'id': section_id, 'chars': end - start})

pathlib.Path(str(out_base) + '.mp3').write_bytes(b'merged-audio')
pathlib.Path(str(out_base) + '.mp3.duration').write_text(str(duration_total))

if failure == 'zero_duration':
    body = (
        cue(1, 0, duration_total, text)
        + cue(2, duration_total, duration_total, 'x')
        + cue(3, duration_total, duration_total, 'x')
        + cue(4, duration_total, duration_total, 'x')
    )
elif failure == 'coverage_gap':
    body = cue(1, 0, duration_total + 5, text)
else:
    body = cue(1, 0, duration_total, text)
pathlib.Path(str(out_base) + '.srt').write_text(body, encoding='utf-8')

take = {
    'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
    'voice': a.voice,
    'model': 'eleven_v3',
    'run_credit_report': {'actual_credits': None if cached else 43},
    'duration_seconds': duration_total,
    'gap_seconds': 0.3,
    'sections': sections,
}
# The child re-derives the SRT timeline from STT on the audio it produced and
# reports how it went. HVP_FAKE_SRT_ALIGNMENT overrides or (as 'omit') drops it.
srt_alignment = os.environ.get('HVP_FAKE_SRT_ALIGNMENT')
if srt_alignment != 'omit':
    take['srt_alignment'] = json.loads(srt_alignment) if srt_alignment else {
        'source': 'stt_forced',
        'sections': n,
        'sections_measured': n,
        'min_anchor_coverage': 0.97,
        'provider_delta_max_seconds': 6.24,
        'sections_with_provider_snapshot': n,
        'stt_calls': n,
    }
# By default report exactly the approved probe's variants applied, matching
# what a real child does with no extra voice-layer rules. Tests that need a
# different shape (missing, empty, or extended) override via env.
if os.environ.get('HVP_FAKE_OMIT_EFFECTIVE_FIXES') != '1':
    explicit_fixes = os.environ.get('HVP_FAKE_EFFECTIVE_FIXES')
    if explicit_fixes is not None:
        take['effective_pronunciation_fixes'] = json.loads(explicit_fixes)
    else:
        overrides = json.loads(pathlib.Path(a.pronunciation_overrides).read_text())
        take['effective_pronunciation_fixes'] = {
            item['term']: item['spoken'] for item in overrides['terms']
        }
pathlib.Path(str(out_base) + '.take.json').write_text(json.dumps(take))
print('OK merged')
"""

FAKE_FFMPEG_SRC = """
import pathlib, shutil, sys

argv = sys.argv[1:]
i = argv.index('-i')
inp = pathlib.Path(argv[i + 1])

if 'volumedetect' in argv:
    db_file = pathlib.Path(str(inp) + '.dbdb')
    db = db_file.read_text().strip() if db_file.exists() else '-20.0'
    sys.stderr.write("[Parsed_volumedetect_0] mean_volume: " + db + " dB\\n")

if '-filter:a' in argv:
    shutil.copyfile(inp, argv[-1])
"""

FAKE_FFPROBE_SRC = """
import pathlib, sys

path = pathlib.Path(sys.argv[-1])
duration_file = pathlib.Path(str(path) + '.duration')
duration = duration_file.read_text().strip() if duration_file.exists() else '3.0'
sys.stdout.write(duration + '\\n')
"""


def _write_fake_ffmpeg_tools(root):
    ffmpeg = root / "fake-ffmpeg"
    ffmpeg.write_text(f"#!{sys.executable}\n{FAKE_FFMPEG_SRC}", encoding="utf-8")
    ffmpeg.chmod(0o755)
    ffprobe = root / "fake-ffprobe"
    ffprobe.write_text(f"#!{sys.executable}\n{FAKE_FFPROBE_SRC}", encoding="utf-8")
    ffprobe.chmod(0o755)
    return ffmpeg, ffprobe


def _generation_env(root, **extra):
    ffmpeg, ffprobe = _write_fake_ffmpeg_tools(root)
    return {
        "HARU_TTS_PYTHON": sys.executable,
        "HARU_FFMPEG": str(ffmpeg),
        "HARU_FFPROBE": str(ffprobe),
        **extra,
    }


def _write_fake_sectioned_generator(tools, generation_calls, *, with_stt_align=True):
    (tools / "narration/generate_sectioned_narration.py").write_text(
        FAKE_SECTIONED_GENERATOR_SRC.replace("__CALLS__", repr(str(generation_calls))),
        encoding="utf-8",
    )
    # The runner pre-flights this module's presence: a checkout without it cannot
    # re-derive the SRT timeline from the audio.
    if with_stt_align:
        (tools / "narration/stt_align.py").write_text("", encoding="utf-8")


def _prepare_reviewed_ab_project(
    root, *, confirm_env=None, mutate_probe=None, narration_text="代送鑑定不能說謊。\n"
):
    """Build a project through an approved ab probe + passing review.

    Returns (project, tools, confirm_calls_path, request_path, request_data).
    """
    project = root / "project"
    tools = root / "media-tools"
    (project / ".hvp/staging").mkdir(parents=True)
    (tools / "narration").mkdir(parents=True)
    narration = project / "narration.txt"
    narration.write_text(narration_text, encoding="utf-8")
    source_sha = hashlib.sha256(narration.read_bytes()).hexdigest()
    plan = project / "pronunciation-plan.json"
    plan.write_text(
        json.dumps({"schema": "haru.pronunciation_plan.v1", "source": {"sha256": source_sha}}),
        encoding="utf-8",
    )
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    (project / ".hvp/pronunciation-analysis.json").write_text(
        json.dumps(
            {
                "schema": "haru.pronunciation_analysis.v1",
                "project": "project",
                "status": "complete",
                "source": "narration.txt",
                "source_sha256": source_sha,
                "output": "pronunciation-plan.json",
                "output_sha256": plan_sha,
            }
        ),
        encoding="utf-8",
    )
    request = project / ".hvp/staging/pronunciation-probe-request.json"
    request_data = {
        "schema": "haru.pronunciation_probe_request.v1",
        "mode": "ab",
        "variants": [
            {"term": "說謊", "spoken": "說恍"},
            {"term": "代送鑑定", "spoken": "代送見定"},
        ],
        "passes": 1,
        "voice": "9lHjugDhwqoxA5MhX0az",
        "model": "eleven_v3",
        "max_credits": 200,
        "spending_approved_by": "harvey",
    }
    request.write_text(json.dumps(request_data, ensure_ascii=False), encoding="utf-8")
    (tools / "narration/g2p_plan.py").write_text(FAKE_G2P_PLAN_SRC, encoding="utf-8")
    calls = tools / "calls"
    (tools / "narration/confirm_pronunciation.py").write_text(
        FAKE_CONFIRM_PRONUNCIATION_SRC.replace("__CALLS__", repr(str(calls))),
        encoding="utf-8",
    )

    confirmed = run(
        "confirm",
        project,
        tools,
        env={
            "HARU_G2PW_PYTHON": sys.executable,
            "HARU_TTS_PYTHON": sys.executable,
            **(confirm_env or {}),
        },
    )
    assert confirmed.returncode == 0, confirmed.stderr
    probe = json.loads(confirmed.stdout)

    if mutate_probe is not None:
        probe = mutate_probe(project, probe)
        (project / ".hvp/pronunciation-probes.json").write_text(
            json.dumps(probe, ensure_ascii=False), encoding="utf-8",
        )

    reviewed = run(
        "review", project, "--reviewed-by", "harvey", "--verdict", "pass",
        "--notes", "G2P B approved",
    )
    assert reviewed.returncode == 0, reviewed.stderr

    (project / ".hvp/staging/narration-generation-request.json").write_text(
        json.dumps(
            {
                "schema": "haru.narration_generation_request.v1",
                "max_credits": 1000,
                "spending_approved_by": "harvey",
            }
        ),
        encoding="utf-8",
    )
    return project, tools, calls, request, request_data


def test_confirm_ab_requires_g2p_matched_variants_before_tts():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, calls, request, request_data = _prepare_reviewed_ab_project(root)

        probe = json.loads((project / ".hvp/pronunciation-probes.json").read_text())
        assert probe["mode"] == "ab"
        assert probe["voice"] == "9lHjugDhwqoxA5MhX0az"
        assert probe["mechanism"] == "inline_respelling"
        assert len(probe["audio"]) == 2
        assert probe["g2p_validation"]["sha256"] == hashlib.sha256(
            (project / probe["g2p_validation"]["path"]).read_bytes()
        ).hexdigest()
        invocation = json.loads(calls.read_text().splitlines()[0])
        assert invocation["ab"] is True
        assert invocation["force_budget"] is True
        assert invocation["candidate"] == ["說謊=說恍", "代送鑑定=代送見定"]

        generation_calls = tools / "generation-calls"
        stale_candidate = project / ".hvp/staging/narration-candidates/stale-candidate"
        stale_candidate.mkdir(parents=True)
        (stale_candidate / "old.mp3").write_bytes(b"old")
        _write_fake_sectioned_generator(tools, generation_calls)

        generated = []
        for _ in range(2):
            full = run(
                "generate-narration", project, tools,
                env=_generation_env(root),
            )
            assert full.returncode == 0, full.stderr
            generated.append(json.loads(full.stdout))
        assert generated[0] == generated[1]
        assert generation_calls.read_text().split()[0] != "missing"
        assert len(generation_calls.read_text().splitlines()) == 1
        full_receipt = generated[0]
        assert full_receipt["schema"] == "haru.narration_generation.v4"
        assert full_receipt["credits_spent"] == 43
        assert full_receipt["generation_mode"] == "sectioned"
        assert full_receipt["speed"] == 1.25
        assert full_receipt["pronunciation_mechanism"] == "inline_respelling"
        assert full_receipt["pronunciation_mechanism_source"] == "from_receipt"
        assert full_receipt["pronunciation_mechanism_evidence"] is None
        assert full_receipt["approved_pronunciation_terms"] == request_data["variants"]
        effective = full_receipt["effective_pronunciation_rules"]
        assert effective["reported_by"] == "child_take_report"
        assert effective["fixes"] == {
            item["term"]: item["spoken"] for item in request_data["variants"]
        }
        assert full_receipt["alignment_check"]["status"] == "pass"
        assert full_receipt["seam_check"]["status"] == "pass"
        assert "dictionary" not in full_receipt["artifacts"]
        assert full_receipt["sections"][0]["chars_cumulative_through_section"] == 9
        assert not stale_candidate.exists()
        assert (project / full_receipt["artifacts"]["audio"]["path"]).read_bytes() == b"merged-audio"
        assert (project / full_receipt["artifacts"]["srt_1x"]["path"]).exists()

        request_data["variants"][0]["spoken"] = "說荒"
        request.write_text(json.dumps(request_data, ensure_ascii=False), encoding="utf-8")
        mismatch = run(
            "confirm",
            project,
            tools,
            env={
                "HARU_G2PW_PYTHON": sys.executable,
                "HARU_TTS_PYTHON": sys.executable,
            },
        )
        assert mismatch.returncode == 2
        assert json.loads(mismatch.stdout)["code"] == "g2p_variant_mismatch"
        assert len(calls.read_text().splitlines()) == 1


def test_generate_narration_pace_gate_rejects_out_of_band_and_accepts_in_band():
    # Below band: total duration too long for the script's char count.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        slow = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="5.0"),
        )
        assert slow.returncode == 2, slow.stdout
        assert json.loads(slow.stdout)["code"] == "narration_pace_out_of_range"

    # Above band: total duration too short for the script's char count.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        fast = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="1.0"),
        )
        assert fast.returncode == 2, fast.stdout
        assert json.loads(fast.stdout)["code"] == "narration_pace_out_of_range"

    # Inside band: passes, and the receipt records mechanism + measured pace.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        ok = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert ok.returncode == 0, ok.stderr
        receipt = json.loads(ok.stdout)
        assert receipt["pronunciation_mechanism"] == "inline_respelling"
        assert receipt["narration_duration_seconds"] == 2.0
        assert receipt["narration_pace_chars_per_second"] == 9 / 2.0
        assert receipt["narration_pace_basis"] == "non_whitespace_characters.v1"
        assert (
            receipt["narration_pace_band"]["min_chars_per_second"]
            <= receipt["narration_pace_chars_per_second"]
            <= receipt["narration_pace_band"]["max_chars_per_second"]
        )

    # Formatting whitespace is not spoken and must not change the pace verdict.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(
            root,
            narration_text="代送鑑定\n\n\n\n不能說謊。\n",
        )
        generation_calls = tools / "generation-calls"
        _write_fake_sectioned_generator(tools, generation_calls)
        formatted = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert formatted.returncode == 0, formatted.stderr
        receipt = json.loads(formatted.stdout)
        assert receipt["narration_pace_chars_per_second"] == 9 / 2.0
        assert receipt["narration_pace_basis"] == "non_whitespace_characters.v1"
        assert receipt["narration_pace_character_count"] == 9

        # Pre-fix receipts did not declare their pace basis and must be
        # regenerated rather than reused under the new calculation.
        receipt_path = project / ".hvp/narration-generation.json"
        legacy_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        legacy_receipt.pop("narration_pace_basis")
        receipt_path.write_text(json.dumps(legacy_receipt), encoding="utf-8")
        regenerated = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert regenerated.returncode == 0, regenerated.stderr
        assert len(generation_calls.read_text().splitlines()) == 2


def test_generate_narration_rejects_truncated_alignment():
    for failure_mode in ("zero_duration", "coverage_gap", "section_cluster"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, tools, *_ = _prepare_reviewed_ab_project(root)
            _write_fake_sectioned_generator(tools, tools / "generation-calls")
            result = run(
                "generate-narration", project, tools,
                env=_generation_env(
                    root, HVP_FAKE_DURATION="2.0", HVP_FAKE_ALIGNMENT_FAILURE=failure_mode,
                ),
            )
            assert result.returncode == 2, (failure_mode, result.stdout)
            assert json.loads(result.stdout)["code"] == "narration_alignment_truncated"
            assert not (project / ".hvp/narration-generation.json").exists()


def _srt_body(cues):
    def fmt(sec):
        ms = round(sec * 1000)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    return "\n".join(
        f"{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n" for i, (start, end, text) in enumerate(cues, 1)
    ) + "\n"


def test_check_alignment_integrity_uses_real_scale_shipped_and_rejected_profiles(tmp_path):
    """Structural cluster detection, not a raw zero-duration ratio, at the
    real scale measured on real artifacts (not a 2-cue toy):

    - shipped/approved: projects/ai-cyber-eval-escape-2026/audio/
      narration-final-1x-approved.srt -- 211 cues, human-approved, exactly 1
      zero-duration cue (a lone trailing "。"). Must pass.
    - rejected/truncated: projects/prepay-card-shop-fraud-taiwan-2026/audio/
      narration-final-1x-single-take-candidate.srt -- 406 cues, 240 of them
      collapsed onto one shared end timestamp at the truncation point. Must
      fail. audio_duration_seconds is set to exactly that shared timestamp,
      same as the real take (whose audio was truncated right there) -- so
      the coverage-tolerance check alone would pass this too, and only the
      cluster check tells the two profiles apart.

    (Those two files are local untracked project artifacts, not committed to
    this repo, so their exact counts are reproduced here rather than read
    from disk -- a worktree or CI checkout would not have them.)
    """
    spec = importlib.util.spec_from_file_location("pronunciation_workflow", SCRIPT)
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)

    approved_cues = [(3.0 * i, 3.0 * i + 2.8, "字") for i in range(210)]
    approved_end = approved_cues[-1][1]
    approved_cues.append((approved_end, approved_end, "。"))
    approved_srt = tmp_path / "approved.srt"
    approved_srt.write_text(_srt_body(approved_cues), encoding="utf-8")
    result = workflow.check_alignment_integrity("approved", approved_srt, approved_end)
    assert result["cue_count"] == 211
    assert result["zero_duration_cue_count"] == 1
    assert result["largest_zero_duration_cluster"] == 1
    assert result["status"] == "pass"

    rejected_cues = [(3.0 * i, 3.0 * i + 2.8, "字") for i in range(166)]
    cutoff = rejected_cues[-1][1]
    rejected_cues += [(cutoff, cutoff, "x") for _ in range(240)]
    rejected_srt = tmp_path / "rejected.srt"
    rejected_srt.write_text(_srt_body(rejected_cues), encoding="utf-8")
    result = workflow.check_alignment_integrity("rejected", rejected_srt, cutoff)
    assert result["cue_count"] == 406
    assert result["zero_duration_cue_count"] == 240
    assert result["largest_zero_duration_cluster"] == 240
    assert result["status"] == "fail"


def test_generate_narration_reports_seam_spread_as_warn_without_failing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0", HVP_FAKE_SECTION_DB="-10,-40"),
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["seam_check"]["status"] == "warn"
        assert receipt["seam_check"]["spread_db"] > receipt["seam_check"]["warn_above_db"]
        assert receipt["checks"]["seam_continuity"] == "warn"


def test_generate_narration_records_child_reported_fixes_beyond_probe_variants():
    # A run whose child applied rules beyond the probe's variants must be
    # recorded as such, not silently reported as probe-only.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_, request_data = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        extra_fixes = {**{v["term"]: v["spoken"] for v in request_data["variants"]},
                       "額外詞": "額外次"}
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(
                root, HVP_FAKE_DURATION="2.0",
                HVP_FAKE_EFFECTIVE_FIXES=json.dumps(extra_fixes, ensure_ascii=False),
            ),
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)

        approved_terms = {v["term"] for v in receipt["approved_pronunciation_terms"]}
        effective = receipt["effective_pronunciation_rules"]
        assert effective["reported_by"] == "child_take_report"
        assert effective["fixes"] == extra_fixes
        # The applied set strictly exceeds the approved probe's terms -- the
        # receipt must expose that, not fold it into approved_pronunciation_terms.
        assert set(effective["fixes"]) - approved_terms == {"額外詞"}


def test_generate_narration_rejects_take_missing_effective_fixes_report():
    # An old child that doesn't report effective_pronunciation_fixes at all
    # cannot produce a receipt here -- unprovable, not "assume probe-only".
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(
                root, HVP_FAKE_DURATION="2.0", HVP_FAKE_OMIT_EFFECTIVE_FIXES="1",
            ),
        )
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "narration_generation_failed"


def test_generate_narration_rejects_empty_effective_fixes_report():
    # A child reporting {} contradicts the approved probe (which always
    # passed non-empty variants in) -- a defect, not a state to record.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(
                root, HVP_FAKE_DURATION="2.0", HVP_FAKE_EFFECTIVE_FIXES="{}",
            ),
        )
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "narration_generation_failed"


def test_generate_narration_rejects_effective_fixes_missing_an_approved_variant():
    # The approved variants were spent real credit money confirming; the
    # child's reported set must actually cover every one of them.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_, request_data = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        partial_fixes = {request_data["variants"][0]["term"]: request_data["variants"][0]["spoken"]}
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(
                root, HVP_FAKE_DURATION="2.0",
                HVP_FAKE_EFFECTIVE_FIXES=json.dumps(partial_fixes, ensure_ascii=False),
            ),
        )
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "narration_generation_failed"


def test_generate_narration_rejects_pronunciation_mechanism_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(
            root,
            confirm_env={"HVP_TEST_B_MECHANISM": "dictionary"},
            mutate_probe=lambda project, probe: {
                key: value for key, value in probe.items() if key != "mechanism"
            },
        )
        result = run(
            "generate-narration", project, tools,
            env={"HARU_TTS_PYTHON": sys.executable},
        )
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "pronunciation_mechanism_mismatch"


def test_generate_narration_derives_mechanism_from_legacy_probe_without_field():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(
            root,
            mutate_probe=lambda project, probe: {
                key: value for key, value in probe.items() if key != "mechanism"
            },
        )
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["pronunciation_mechanism"] == "inline_respelling"
        assert receipt["pronunciation_mechanism_source"] == "derived_from_take"
        assert receipt["pronunciation_mechanism_evidence"]["take_sha256"] == hashlib.sha256(
            (project / receipt["pronunciation_mechanism_evidence"]["take_path"]).read_bytes()
        ).hexdigest()
        assert receipt["pronunciation_mechanism_evidence"]["text_sha256"] == hashlib.sha256(
            (project / receipt["pronunciation_mechanism_evidence"]["text_path"]).read_bytes()
        ).hexdigest()


def _stage_narration_request(project, **extra):
    (project / ".hvp/staging/narration-generation-request.json").write_text(
        json.dumps(
            {
                "schema": "haru.narration_generation_request.v1",
                "max_credits": 1000,
                "spending_approved_by": "harvey",
                **extra,
            }
        ),
        encoding="utf-8",
    )


def test_generate_narration_uses_requested_section_sizing():
    # Every seam is an independent TTS request, so the model restarts prosody
    # there. Section sizing is the lever for that, and it has to be tunable per
    # take without editing the runner.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _stage_narration_request(
            project, section_target_chars=1200, section_max_chars=1500,
        )
        generation_calls = tools / "generation-calls"
        _write_fake_sectioned_generator(tools, generation_calls)
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["target_chars"] == 1200
        assert receipt["max_chars"] == 1500
        assert generation_calls.read_text().split()[-1] == "1200/1500"

        # The inequality is the whole rule: a target-only override below the
        # default max is consistent, so it applies rather than being rejected.
        _stage_narration_request(project, section_target_chars=400)
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["target_chars"] == 400
        assert generation_calls.read_text().split()[-1] == "400/520"


def test_generate_narration_regenerates_when_section_sizing_changes():
    # A cached receipt must never claim a sizing that a differently-sized run
    # produced. request_sha covers the whole staged file, so restaging with new
    # sizing has to bust both the receipt reuse key and the candidate job_sha.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        generation_calls = tools / "generation-calls"
        _write_fake_sectioned_generator(tools, generation_calls)
        env = _generation_env(root, HVP_FAKE_DURATION="2.0")

        first = run("generate-narration", project, tools, env=env)
        assert first.returncode == 0, first.stderr
        assert json.loads(first.stdout)["max_chars"] == 520

        _stage_narration_request(
            project, section_target_chars=1200, section_max_chars=1500,
        )
        second = run("generate-narration", project, tools, env=env)
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout)["max_chars"] == 1500
        assert len(generation_calls.read_text().splitlines()) == 2


def test_generate_narration_rejects_a_take_whose_sections_ignored_max_chars():
    # The receipt's max_chars has to describe the take. A child that ignores the
    # flag would otherwise hand back sections long enough to voice silently
    # short, under a receipt claiming they were bounded.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(
                root, HVP_FAKE_DURATION="2.0", HVP_FAKE_OVERSIZED_SECTION="600",
            ),
        )
        assert result.returncode == 2, result.stdout
        payload = json.loads(result.stdout)
        assert payload["code"] == "narration_generation_failed"


def test_generate_narration_rejects_section_sizing_outside_measured_safe_range():
    # eleven_v3 silently truncated a 4513-char request after voicing 1754 chars
    # while still claiming full alignment coverage. A section long enough to hit
    # that must not be reachable through a staged request.
    spec = importlib.util.spec_from_file_location("pronunciation_workflow", SCRIPT)
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    for sizing in (
        {"section_max_chars": workflow.NARRATION_SECTION_CHARS_CEILING + 1},
        {"section_target_chars": 1500, "section_max_chars": 1200},
        {"section_target_chars": 10},
        {"section_target_chars": "1200"},
        {"section_target_chars": 300.0},
        # Explicit null is an error, not "take the default" -- request.get()
        # only falls back when the key is absent.
        {"section_target_chars": None},
        # Raising the target alone leaves max_chars at its 520 default, so the
        # request is inconsistent rather than partially applied.
        {"section_target_chars": 1200},
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, tools, *_ = _prepare_reviewed_ab_project(root)
            _stage_narration_request(project, **sizing)
            generation_calls = tools / "generation-calls"
            _write_fake_sectioned_generator(tools, generation_calls)
            result = run(
                "generate-narration", project, tools,
                env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
            )
            assert result.returncode == 2, (sizing, result.stdout)
            assert json.loads(result.stdout)["code"] == "invalid_narration_request"
            assert not generation_calls.exists(), sizing


def test_generate_narration_records_where_the_srt_timeline_came_from():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["srt_alignment"]["source"] == "stt_forced"
        # The drift the timestamp-vs-duration gates cannot see: a timeline can
        # be several seconds out in the middle and still start and end right.
        assert receipt["srt_alignment"]["provider_delta_max_seconds"] == 6.24
        assert receipt["checks"]["srt_alignment_source"] == "pass"


def test_generate_narration_refuses_a_take_timed_by_the_provider():
    # eleven_v3's own alignment is the thing measured to drift, and the SRT pins
    # the cue-driven visual cuts. A child too old to re-derive the timeline
    # cannot generate through this runner: unprovable, not "probably fine".
    for alignment in (
        "omit",
        json.dumps({"source": "provider_alignment", "sections": 1}),
        # Coverage null because a section went unmeasured -- must not be read
        # as "measured and fine", and must not blow up on the comparison.
        json.dumps({
            "source": "stt_forced", "sections": 2, "sections_measured": 1,
            "min_anchor_coverage": None,
        }),
        json.dumps({
            "source": "stt_forced", "sections": 1, "sections_measured": 1,
            "min_anchor_coverage": True,
        }),
        # A real number, but taken over only the sections that were measured.
        # The unmeasured one is not covered by it and must not be spoken for.
        json.dumps({
            "source": "stt_forced", "sections": 2, "sections_measured": 1,
            "min_anchor_coverage": 0.99,
        }),
        json.dumps({
            "source": "stt_forced", "sections": 0, "sections_measured": 0,
            "min_anchor_coverage": 1.0,
        }),
        # Self-consistent, but not the take's real section count: the fake child
        # emits two sections here, so "1 of 1" measures one and speaks for both.
        json.dumps({
            "source": "stt_forced", "sections": 1, "sections_measured": 1,
            "min_anchor_coverage": 0.99,
        }),
        # NaN passes every comparison it is given, so it would read as
        # "measured and fine" -- and it is not valid JSON for the Rust reader.
        '{"source": "stt_forced", "sections": 2, "sections_measured": 2, '
        '"min_anchor_coverage": NaN}',
        '{"source": "stt_forced", "sections": 2, "sections_measured": 2, '
        '"min_anchor_coverage": 0.99, "provider_delta_max_seconds": Infinity}',
        # Absence of evidence arriving as a confident 0.0 is what the child's
        # null discipline exists to prevent; the receipt enforces it too.
        json.dumps({
            "source": "stt_forced", "sections": 2, "sections_measured": 2,
            "min_anchor_coverage": 0.99, "provider_delta_max_seconds": -1.0,
        }),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, tools, *_ = _prepare_reviewed_ab_project(root)
            _write_fake_sectioned_generator(tools, tools / "generation-calls")
            result = run(
                "generate-narration", project, tools,
                env=_generation_env(
                    root, HVP_FAKE_DURATION="2.0",
                    # Two sections, so a self-consistent "1 of 1" report is
                    # distinguishable from one that matches the real count.
                    HVP_FAKE_SECTION_DB="-20,-20",
                    HVP_FAKE_SRT_ALIGNMENT=alignment,
                ),
            )
            assert result.returncode == 2, (alignment, result.stdout)
            assert json.loads(result.stdout)["code"] == "narration_generation_failed"
            assert not (project / ".hvp/narration-generation.json").exists()


def test_a_receipt_from_before_the_timeline_gate_is_not_reusable():
    # A receipt written when the SRT still carried the provider's timings
    # describes a take timed by the alignment that was measured to drift. It
    # must not be handed back as a cache hit.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        generation_calls = tools / "generation-calls"
        _write_fake_sectioned_generator(tools, generation_calls)
        env = _generation_env(root, HVP_FAKE_DURATION="2.0")

        assert run("generate-narration", project, tools, env=env).returncode == 0
        assert len(generation_calls.read_text().splitlines()) == 1

        receipt_path = project / ".hvp/narration-generation.json"
        receipt = json.loads(receipt_path.read_text())
        del receipt["srt_alignment"]
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

        result = run("generate-narration", project, tools, env=env)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["srt_alignment"]["source"] == "stt_forced"
        assert len(generation_calls.read_text().splitlines()) == 2


def test_a_boolean_section_count_is_not_a_section_count():
    # bool is an int subclass and `True == 1`, so on a single-section take an
    # unguarded numeric check would accept {"sections": true} and write it
    # verbatim into a durable receipt.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(
                root, HVP_FAKE_DURATION="2.0",
                HVP_FAKE_SRT_ALIGNMENT=json.dumps({
                    "source": "stt_forced", "sections": True,
                    "sections_measured": True, "min_anchor_coverage": 0.99,
                }),
            ),
        )
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "narration_generation_failed"


def test_a_checkout_that_cannot_re_time_fails_before_spending():
    # A haru-media-tools checkout without stt_align.py produces a take that
    # would be refused anyway -- but only after ElevenLabs had been billed for
    # it. The missing module is knowable before the subprocess runs.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        generation_calls = tools / "generation-calls"
        _write_fake_sectioned_generator(tools, generation_calls, with_stt_align=False)
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "invalid_path"
        assert not generation_calls.exists(), "refused after paying to generate"


def test_generate_narration_refuses_a_timeline_too_few_characters_anchored():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(
                root, HVP_FAKE_DURATION="2.0",
                HVP_FAKE_SRT_ALIGNMENT=json.dumps({
                    "source": "stt_forced", "sections": 1, "sections_measured": 1,
                    "min_anchor_coverage": 0.5,
                }),
            ),
        )
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "narration_srt_alignment_unreliable"


def test_generate_narration_rejects_take_exceeding_max_credits():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        (project / ".hvp/staging/narration-generation-request.json").write_text(
            json.dumps(
                {
                    "schema": "haru.narration_generation_request.v1",
                    "max_credits": 40,
                    "spending_approved_by": "harvey",
                }
            ),
            encoding="utf-8",
        )
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
        )
        assert result.returncode == 2, result.stdout
        payload = json.loads(result.stdout)
        assert payload["code"] == "narration_generation_failed"


def test_generate_narration_treats_fully_cached_rerun_as_zero_credits():
    # generate_sectioned_narration.py reports actual_credits=null when every
    # section was already cached. Before this fix, that state made
    # generate-narration fail forever -- sections stay cached, the receipt is
    # never written, and every retry hit the same "invalid take" error.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, tools, *_ = _prepare_reviewed_ab_project(root)
        _write_fake_sectioned_generator(tools, tools / "generation-calls")
        result = run(
            "generate-narration", project, tools,
            env=_generation_env(root, HVP_FAKE_DURATION="2.0", HVP_FAKE_CACHED="1"),
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["credits_spent"] == 0


def _promoted_project(root, **request_overrides):
    """Generate a narration candidate, then stage its acceptance for promotion."""
    project, tools, *_ = _prepare_reviewed_ab_project(root)
    _write_fake_sectioned_generator(tools, tools / "generation-calls")
    generated = run(
        "generate-narration", project, tools,
        env=_generation_env(root, HVP_FAKE_DURATION="2.0"),
    )
    assert generated.returncode == 0, generated.stderr
    receipt = json.loads(generated.stdout)
    request = {
        "schema": "haru.narration_promotion_request.v1",
        "accepted_by": "harvey",
        "candidate_audio": receipt["artifacts"]["audio"]["path"],
        "audio_sha256": receipt["artifacts"]["audio"]["sha256"],
        "accepted_issues": [{"text": "說謊", "note": "minor, judged acceptable"}],
        **request_overrides,
    }
    (project / ".hvp/staging/narration-promotion-request.json").write_text(
        json.dumps(request, ensure_ascii=False), encoding="utf-8",
    )
    return project, receipt


def test_promotion_makes_the_accepted_candidate_canonical():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, receipt = _promoted_project(root)
        result = run("promote-narration", project)
        assert result.returncode == 0, result.stderr
        promotion = json.loads(result.stdout)

        assert promotion["schema"] == "haru.narration_promotion.v1"
        assert promotion["accepted_by"] == "harvey"
        candidate = project / receipt["artifacts"]["audio"]["path"]
        final = project / "narration-final.mp3"
        assert final.read_bytes() == candidate.read_bytes()
        assert (project / "narration-final.srt").is_file()
        # The audio the human accepted is the audio now standing as canonical.
        assert promotion["audio_sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()

        # The stamp is a claim about particular bytes, so it is rewritten for
        # these bytes rather than carried forward from the previous take.
        stamp = json.loads((project / "narration-final.mp3.pron-ok.json").read_text())
        assert stamp["sha256"] == promotion["audio_sha256"]
        assert stamp["approved_by"] == "harvey"
        assert stamp["accepted_issues"][0]["disposition"] == "accepted_by_human"


def test_promotion_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, _ = _promoted_project(root)
        first = run("promote-narration", project)
        second = run("promote-narration", project)
        assert first.returncode == 0 and second.returncode == 0, second.stderr
        assert json.loads(first.stdout) == json.loads(second.stdout)


def test_promotion_refuses_audio_the_human_did_not_accept():
    # The accepted sha256, the receipt's sha256, and the bytes on disk must all
    # be the same audio. A human can only accept audio they heard, and only
    # audio that passed the gates can be promoted.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, _ = _promoted_project(root, audio_sha256="0" * 64)
        result = run("promote-narration", project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "promotion_candidate_mismatch"
        assert not (project / "narration-final.mp3").exists()


def test_promotion_refuses_when_the_bytes_changed_after_acceptance():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, receipt = _promoted_project(root)
        (project / receipt["artifacts"]["audio"]["path"]).write_bytes(b"swapped-after-approval")
        result = run("promote-narration", project)
        assert result.returncode == 2, result.stdout
        # Caught by the receipt validator: an artifact whose bytes changed means
        # the receipt no longer describes what is on disk, gates included.
        assert json.loads(result.stdout)["code"] == "narration_receipt_stale"
        assert not (project / "narration-final.mp3").exists()


def test_promotion_refuses_a_candidate_the_receipt_does_not_describe():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, receipt = _promoted_project(root)
        # Real file, real digest, but not the audio this receipt gated.
        other = project / ".hvp/staging/other.mp3"
        other.write_bytes(b"a different take entirely")
        request = json.loads(
            (project / ".hvp/staging/narration-promotion-request.json").read_text())
        request["candidate_audio"] = ".hvp/staging/other.mp3"
        request["audio_sha256"] = hashlib.sha256(other.read_bytes()).hexdigest()
        (project / ".hvp/staging/narration-promotion-request.json").write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8")
        result = run("promote-narration", project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "promotion_candidate_mismatch"


def test_promotion_refuses_after_the_script_was_edited():
    # The receipt describes a take of the script as it was. If the narration has
    # been edited since, the audio no longer says what the project says.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, _ = _promoted_project(root)
        (project / "narration.txt").write_text("完全不同的稿子。\n", encoding="utf-8")
        result = run("promote-narration", project)
        assert result.returncode == 2, result.stdout
        # The plan, the probe and the review are all bound to the old script, so
        # the chain breaks at its first link rather than at the receipt.
        assert json.loads(result.stdout)["code"] == "pronunciation_plan_stale"
        assert not (project / "narration-final.mp3").exists()


def test_promotion_refuses_a_request_without_a_named_human():
    for override in (
        {"accepted_by": "   "},
        {"accepted_by": 42},
        {"schema": "haru.narration_promotion_request.v0"},
        {"audio_sha256": "not-a-digest"},
        {"accepted_issues": [{"text": "說謊"}]},
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _ = _promoted_project(root, **override)
            result = run("promote-narration", project)
            assert result.returncode == 2, (override, result.stdout)
            assert json.loads(result.stdout)["code"] == "invalid_promotion_request"
            assert not (project / "narration-final.mp3").exists()


def test_promotion_refuses_a_copy_of_the_right_audio_at_the_wrong_path():
    # Identical bytes, so every digest check passes -- but promoting from an
    # arbitrary path drops the binding that says the receipt gated THIS
    # artifact, and the copy need not live where the receipt put it.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, receipt = _promoted_project(root)
        original = project / receipt["artifacts"]["audio"]["path"]
        copy = project / ".hvp/staging/copy-of-the-same-audio.mp3"
        copy.write_bytes(original.read_bytes())
        request = json.loads(
            (project / ".hvp/staging/narration-promotion-request.json").read_text())
        request["candidate_audio"] = ".hvp/staging/copy-of-the-same-audio.mp3"
        (project / ".hvp/staging/narration-promotion-request.json").write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8")
        result = run("promote-narration", project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "promotion_candidate_mismatch"
        assert not (project / "narration-final.mp3").exists()


def test_promotion_refuses_when_the_subtitle_changed_after_gating():
    # The SRT ships and pins the cue-driven cuts, so it is bound as tightly as
    # the audio -- an SRT edited after the gates ran is not the one they passed.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, receipt = _promoted_project(root)
        (project / receipt["artifacts"]["srt"]["path"]).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n改過的字幕\n\n", encoding="utf-8")
        result = run("promote-narration", project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "narration_receipt_stale"
        assert not (project / "narration-final.mp3").exists()


def test_promotion_refuses_a_receipt_that_did_not_pass_the_gates():
    """The natural bad path, not a contrived one.

    A take that fails the truncation gate leaves a plausible-looking MP3 on disk
    with no receipt written. "Repair the missing receipt" is what a confused
    agent does next -- and a promotion that trusted the receipt's own claim of
    completeness would then make an ungated take canonical, with a freshly
    written clean pronunciation stamp on top of it.
    """
    for damage in (
        {"alignment_check": {"status": "fail"}},
        {"srt_alignment": {"source": "provider_alignment"}},
        {"credits_spent": 999_999},
        {"effective_pronunciation_rules": {"reported_by": "hand_written", "fixes": {}}},
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _ = _promoted_project(root)
            receipt_path = project / ".hvp/narration-generation.json"
            receipt = json.loads(receipt_path.read_text())
            receipt.update(damage)
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

            result = run("promote-narration", project)
            assert result.returncode == 2, (damage, result.stdout)
            assert json.loads(result.stdout)["code"] == "narration_receipt_stale"
            assert not (project / "narration-final.mp3").exists()


def test_promotion_retires_a_storyboard_timed_to_the_previous_narration():
    # time_storyboard.py derives the timed storyboard from narration-final.srt
    # and records no digest of it, and the layout gate accepts the validation
    # file on ok:true alone -- so a stale timed storyboard would render the new
    # audio against the old cue times with every gate passing.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, _ = _promoted_project(root)
        (project / "narration-final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n舊\n\n",
                                                     encoding="utf-8")
        (project / "storyboard-final-timed.json").write_text("{}", encoding="utf-8")
        (project / "storyboard-final-timed-validation.json").write_text('{"ok": true}',
                                                                        encoding="utf-8")

        promotion = json.loads(run("promote-narration", project).stdout)
        assert sorted(promotion["retired_timed_storyboard"]) == [
            "storyboard-final-timed-validation.json",
            "storyboard-final-timed.json",
        ]
        assert not (project / "storyboard-final-timed.json").exists()
        # Renamed, not destroyed: the old timings are the best starting point
        # for re-timing.
        assert list(project.glob("storyboard-final-timed.json.stale-narration-*"))


def test_promotion_keeps_a_storyboard_timed_to_the_same_narration():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, _ = _promoted_project(root)
        assert run("promote-narration", project).returncode == 0
        (project / "storyboard-final-timed.json").write_text("{}", encoding="utf-8")

        # Re-promoting the same audio must not retire a storyboard timed to it.
        (project / ".hvp/narration-promotion.json").unlink()
        promotion = json.loads(run("promote-narration", project).stdout)
        assert promotion["retired_timed_storyboard"] == []
        assert (project / "storyboard-final-timed.json").is_file()


def test_promotion_refuses_when_the_human_failed_the_pronunciation_review():
    # A hand-repaired chain: the review is rewritten to a FAIL verdict and the
    # receipt's review digest is updated to match, so every digest lines up and
    # only the verdict itself is wrong. That is what "fix the receipt until it
    # validates" produces.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, _ = _promoted_project(root)
        review_path = project / ".hvp/pronunciation-review.json"
        review = json.loads(review_path.read_text())
        review["verdict"] = "fail"
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

        receipt_path = project / ".hvp/narration-generation.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["review_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

        result = run("promote-narration", project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "pronunciation_review_stale"
        assert not (project / "narration-final.mp3").exists()


def test_promotion_refuses_a_symlinked_canonical_target():
    # Before and after a successful promotion: re-running must not be a way to
    # keep a symlinked canonical name that a first promotion would have refused.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project, _ = _promoted_project(root)
        elsewhere = project / "elsewhere.mp3"
        elsewhere.write_bytes(b"not the accepted audio")

        (project / "narration-final.mp3").symlink_to(elsewhere)
        first = run("promote-narration", project)
        assert first.returncode == 2, first.stdout
        assert json.loads(first.stdout)["code"] == "invalid_path"

        (project / "narration-final.mp3").unlink()
        assert run("promote-narration", project).returncode == 0
        (project / "narration-final.mp3").unlink()
        (project / "narration-final.mp3").symlink_to(elsewhere)
        second = run("promote-narration", project)
        assert second.returncode == 2, second.stdout
        assert json.loads(second.stdout)["code"] == "invalid_path"
