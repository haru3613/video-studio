use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::Artifact;
use crate::application::direct_file;

pub(crate) const SEGMENT_IDS: [&str; 4] = ["qi", "cheng", "zhuan", "he"];
pub(crate) const SEGMENT_OUTPUTS: [&str; 4] = [
    "output/segments/01-qi.mp4",
    "output/segments/02-cheng.mp4",
    "output/segments/03-zhuan.mp4",
    "output/segments/04-he.mp4",
];

const INPUTS: [(&str, &str); 4] = [
    ("storyboard", "storyboard-final-timed.json"),
    ("editorial_contract", "editorial-contract.json"),
    ("narration", "narration-final.mp3"),
    ("srt", "narration-final.srt"),
];
pub(crate) const ASSEMBLY_OUTPUT: &str = "output/final.pre-loudnorm.mp4";
pub(crate) const ASSEMBLY_RECEIPT: &str = "quality-review/segments/assembly.json";
const EPSILON: f64 = 0.000_001;

#[derive(Debug, Clone)]
pub(crate) struct SegmentBinding {
    pub id: &'static str,
    pub output: &'static str,
    pub definition_sha256: String,
    pub dependency_sha256: String,
    pub selector: Value,
}

#[derive(Debug)]
pub(crate) struct SegmentAuthority {
    pub segments: Vec<SegmentBinding>,
    assembly_policy_sha256: String,
}

#[derive(Debug, Clone)]
struct Cue {
    index: usize,
    start: f64,
    end: f64,
    text: String,
}

fn digest_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn update_tagged_len(hasher: &mut Sha256, tag: u8, length: usize) {
    hasher.update([tag]);
    hasher.update(length.to_string().as_bytes());
    hasher.update(b":");
}

fn canonical_string(hasher: &mut Sha256, value: &str) {
    update_tagged_len(hasher, b's', value.len());
    hasher.update(value.as_bytes());
}

fn canonical_update(hasher: &mut Sha256, value: &Value) -> Option<()> {
    match value {
        Value::Null => hasher.update(b"n"),
        Value::Bool(true) => hasher.update(b"t"),
        Value::Bool(false) => hasher.update(b"f"),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                let payload = value.to_string();
                update_tagged_len(hasher, b'i', payload.len());
                hasher.update(payload.as_bytes());
            } else if let Some(value) = number.as_u64() {
                let payload = value.to_string();
                update_tagged_len(hasher, b'i', payload.len());
                hasher.update(payload.as_bytes());
            } else {
                let value = number.as_f64()?;
                if !value.is_finite() {
                    return None;
                }
                hasher.update(b"d");
                hasher.update(value.to_be_bytes());
            }
        }
        Value::String(value) => canonical_string(hasher, value),
        Value::Array(values) => {
            update_tagged_len(hasher, b'a', values.len());
            for value in values {
                canonical_update(hasher, value)?;
            }
        }
        Value::Object(values) => {
            let mut entries = values.iter().collect::<Vec<_>>();
            entries.sort_by(|(left, _), (right, _)| left.as_bytes().cmp(right.as_bytes()));
            update_tagged_len(hasher, b'o', entries.len());
            for (key, value) in entries {
                canonical_string(hasher, key);
                canonical_update(hasher, value)?;
            }
        }
    }
    Some(())
}

fn canonical_digest(value: &Value) -> Option<String> {
    let mut hasher = Sha256::new();
    canonical_update(&mut hasher, value)?;
    Some(format!("{:x}", hasher.finalize()))
}

fn read_json(path: &Path) -> Option<Value> {
    let path = direct_file(path).ok()?;
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_frame_rate(value: &str) -> bool {
    let mut parts = value.split('/');
    matches!(
        (parts.next(), parts.next(), parts.next()),
        (Some(numerator), Some(denominator), None)
            if numerator.parse::<u64>().is_ok_and(|value| value > 0)
                && denominator.parse::<u64>().is_ok_and(|value| value > 0)
    )
}

fn has_exact_keys(object: &serde_json::Map<String, Value>, keys: &[&str]) -> bool {
    object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
}

fn stream_profile_valid(value: &Value) -> bool {
    let Some(streams) = value
        .as_object()
        .and_then(|profile| profile.get("streams"))
        .and_then(Value::as_array)
    else {
        return false;
    };
    if streams.len() != 2 {
        return false;
    }
    let Some(video) = streams[0].as_object() else {
        return false;
    };
    if !has_exact_keys(
        video,
        &[
            "codec_type",
            "codec_name",
            "codec_tag_string",
            "extradata_hash",
            "time_base",
            "width",
            "height",
            "pix_fmt",
            "field_order",
            "sample_aspect_ratio",
            "frame_rate",
            "color_range",
            "color_space",
            "color_transfer",
            "color_primaries",
        ],
    ) {
        return false;
    }
    if video.get("codec_type").and_then(Value::as_str) != Some("video")
        || video.get("codec_name").and_then(Value::as_str).is_none()
        || video.get("width").and_then(Value::as_u64) == Some(0)
        || video.get("width").and_then(Value::as_u64).is_none()
        || video.get("height").and_then(Value::as_u64) == Some(0)
        || video.get("height").and_then(Value::as_u64).is_none()
        || video.get("pix_fmt").and_then(Value::as_str).is_none()
        || !video
            .get("frame_rate")
            .and_then(Value::as_str)
            .is_some_and(valid_frame_rate)
    {
        return false;
    }
    let Some(audio) = streams[1].as_object() else {
        return false;
    };
    if !has_exact_keys(
        audio,
        &[
            "codec_type",
            "codec_name",
            "codec_tag_string",
            "extradata_hash",
            "time_base",
            "sample_rate",
            "channels",
            "channel_layout",
            "sample_fmt",
        ],
    ) {
        return false;
    }
    audio.get("codec_type").and_then(Value::as_str) == Some("audio")
        && audio.get("codec_name").and_then(Value::as_str).is_some()
        && audio
            .get("sample_rate")
            .and_then(Value::as_u64)
            .is_some_and(|value| value > 0)
        && audio
            .get("channels")
            .and_then(Value::as_u64)
            .is_some_and(|value| value > 0)
        && audio
            .get("channel_layout")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
}

fn source_stream_profile_valid(value: &Value) -> bool {
    value
        .as_object()
        .is_some_and(|profile| has_exact_keys(profile, &["streams"]))
        && stream_profile_valid(value)
}

fn expected_target_profile(profile: &Value) -> Option<Value> {
    let streams = profile.as_object()?.get("streams")?.as_array()?;
    let video = streams.first()?.as_object()?;
    let audio = streams.get(1).and_then(Value::as_object);
    Some(serde_json::json!({
        "container": "mp4",
        "video": {
            "codec_name": "h264",
            "width": video.get("width")?,
            "height": video.get("height")?,
            "pix_fmt": video.get("pix_fmt")?,
            "frame_rate": video.get("frame_rate")?,
        },
        "audio": audio.map(|audio| serde_json::json!({
            "codec_name": "aac",
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
            "channel_layout": audio.get("channel_layout"),
        })),
    }))
}

fn output_stream_profile_valid(
    profile: &Value,
    target: &Value,
    receipt_duration: f64,
    require_target_match: bool,
) -> bool {
    if !profile
        .as_object()
        .is_some_and(|value| has_exact_keys(value, &["streams", "duration_seconds"]))
        || !stream_profile_valid(profile)
    {
        return false;
    }
    let Some(streams) = profile
        .as_object()
        .and_then(|value| value.get("streams"))
        .and_then(Value::as_array)
    else {
        return false;
    };
    let Some(duration) = profile
        .as_object()
        .and_then(|value| value.get("duration_seconds"))
        .and_then(Value::as_f64)
    else {
        return false;
    };
    let Some(target) = target.as_object() else {
        return false;
    };
    let (Some(video), Some(audio), Some(target_video), Some(target_audio)) = (
        streams.first().and_then(Value::as_object),
        streams.get(1).and_then(Value::as_object),
        target.get("video").and_then(Value::as_object),
        target.get("audio").and_then(Value::as_object),
    ) else {
        return false;
    };
    duration.is_finite()
        && duration > 0.0
        && close(duration, receipt_duration)
        && target.get("container").and_then(Value::as_str) == Some("mp4")
        && (!require_target_match
            || (["codec_name", "width", "height", "pix_fmt", "frame_rate"]
                .iter()
                .all(|key| video.get(*key) == target_video.get(*key))
                && ["codec_name", "sample_rate", "channels", "channel_layout"]
                    .iter()
                    .all(|key| audio.get(*key) == target_audio.get(*key))))
}

fn target_frame_tolerance(target: &Value) -> Option<f64> {
    let rate = target
        .as_object()?
        .get("video")?
        .as_object()?
        .get("frame_rate")?
        .as_str()?;
    let mut parts = rate.split('/');
    let numerator = parts.next()?.parse::<u64>().ok()?;
    let denominator = parts.next()?.parse::<u64>().ok()?;
    if numerator == 0 || denominator == 0 || parts.next().is_some() {
        return None;
    }
    Some(denominator as f64 / numerator as f64)
}

fn close(left: f64, right: f64) -> bool {
    (left - right).abs() <= EPSILON
}

fn frame_index(seconds: f64, fps: u64) -> Option<u64> {
    let frame = seconds * fps as f64;
    if !frame.is_finite() || frame < 0.0 || frame > u64::MAX as f64 {
        return None;
    }
    Some(frame.round_ties_even() as u64)
}

fn srt_seconds(value: &str) -> Option<f64> {
    let normalized = value.trim().replace('.', ",");
    let mut clock = normalized.split(':');
    let hours = clock.next()?.parse::<u64>().ok()?;
    let minutes = clock.next()?.parse::<u64>().ok()?;
    let mut second_parts = clock.next()?.split(',');
    let seconds = second_parts.next()?.parse::<u64>().ok()?;
    let millis = second_parts.next()?.parse::<u64>().ok()?;
    if clock.next().is_some() || second_parts.next().is_some() {
        return None;
    }
    Some((hours * 3600 + minutes * 60 + seconds) as f64 + millis as f64 / 1000.0)
}

fn parse_srt(bytes: &[u8]) -> Option<Vec<Cue>> {
    let text = std::str::from_utf8(bytes)
        .ok()?
        .trim()
        .replace("\r\n", "\n");
    let mut cues = Vec::new();
    for (offset, block) in text.split("\n\n").enumerate() {
        let lines = block.lines().collect::<Vec<_>>();
        if lines.len() < 3 || !lines[2..].iter().any(|line| !line.trim().is_empty()) {
            return None;
        }
        let (start, end) = lines.get(1)?.split_once("-->")?;
        let start = srt_seconds(start)?;
        let end = srt_seconds(end)?;
        if end < start {
            return None;
        }
        if close(start, end) {
            continue;
        }
        cues.push(Cue {
            index: offset + 1,
            start,
            end,
            text: lines[2..].join("\n"),
        });
    }
    (!cues.is_empty()).then_some(cues)
}

fn required_object<'a>(value: &'a Value, keys: &[&str]) -> Option<&'a Map<String, Value>> {
    let object = value.as_object()?;
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let expected = keys.iter().copied().collect::<BTreeSet<_>>();
    (actual == expected).then_some(object)
}

fn string_list(value: &Value) -> Option<Vec<&str>> {
    let values = value.as_array()?;
    if values.is_empty() {
        return None;
    }
    values.iter().map(Value::as_str).collect()
}

fn locality_value(plan: &Value, segments: &[Value]) -> Option<Value> {
    let assembly = plan.get("assembly")?;
    let mut locality = Vec::with_capacity(segments.len());
    for segment in segments {
        let mut item = Map::new();
        for key in [
            "segment_id",
            "ordinal",
            "start",
            "end",
            "scene_ids",
            "event_ids",
            "selector",
            "output",
        ] {
            item.insert(key.to_owned(), segment.get(key)?.clone());
        }
        locality.push(Value::Object(item));
    }
    Some(serde_json::json!({"segments": locality, "assembly": assembly}))
}

impl SegmentAuthority {
    pub(crate) fn load(project: &Path) -> Option<Self> {
        let plan = read_json(&project.join("segment-plan.json"))?;
        if plan.get("schema")?.as_str()? != "haru.segment_plan.v1"
            || plan.get("project")?.as_str()? != project.file_name()?.to_str()?
        {
            return None;
        }

        let bindings = plan.get("inputs")?.as_object()?;
        if bindings.len() != INPUTS.len() {
            return None;
        }
        let mut current_inputs = Map::new();
        let mut source_bytes = std::collections::BTreeMap::new();
        for (name, relative) in INPUTS {
            let binding = required_object(bindings.get(name)?, &["path", "sha256"])?;
            let supplied = binding.get("sha256")?.as_str()?;
            if binding.get("path")?.as_str()? != relative || !valid_sha256(supplied) {
                return None;
            }
            let source = direct_file(&project.join(relative)).ok()?;
            let bytes = fs::read(source).ok()?;
            let actual = digest_bytes(&bytes);
            if actual != supplied {
                return None;
            }
            current_inputs.insert(
                name.to_owned(),
                serde_json::json!({"path": relative, "sha256": actual}),
            );
            source_bytes.insert(name, bytes);
        }
        let render_input_sha256 = canonical_digest(&serde_json::json!({
            "schema": "haru.segment_render_inputs.v1",
            "inputs": current_inputs,
        }))?;
        if plan.get("render_input_sha256")?.as_str()? != render_input_sha256 {
            return None;
        }

        let segments = plan.get("segments")?.as_array()?;
        if segments.len() != SEGMENT_IDS.len() {
            return None;
        }
        let assembly = required_object(
            plan.get("assembly")?,
            &["order", "transition_policy", "audio_policy"],
        )?;
        if assembly.get("order")? != &serde_json::json!(SEGMENT_IDS)
            || assembly.get("transition_policy")?.as_str()? != "cut.v1"
            || assembly.get("audio_policy")?.as_str()? != "premix.v1"
        {
            return None;
        }
        let assembly_policy_sha256 = canonical_digest(&serde_json::json!({
            "schema": "haru.segment_assembly_policy.v1",
            "order": SEGMENT_IDS,
            "transition_policy": "cut.v1",
            "audio_policy": "premix.v1",
        }))?;

        let storyboard: Value = serde_json::from_slice(source_bytes.get("storyboard")?).ok()?;
        if storyboard.get("schema")?.as_str()? != "haru.storyboard_timed.v1" {
            return None;
        }
        let mut storyboard_authority = storyboard.as_object()?.clone();
        storyboard_authority.remove("scenes");
        let storyboard_scenes = storyboard.get("scenes")?.as_array()?;
        if storyboard_scenes.is_empty() {
            return None;
        }
        let mut seen_scene_ids = BTreeSet::new();
        let mut seen_event_ids = BTreeSet::new();
        for scene in storyboard_scenes {
            let scene_id = scene.get("scene_id")?.as_str()?;
            let scene_start = scene.get("start_seconds")?.as_f64()?;
            let scene_end = scene.get("end_seconds")?.as_f64()?;
            let events = scene.get("visual_events")?.as_array()?;
            if scene_id.is_empty()
                || !seen_scene_ids.insert(scene_id)
                || scene_end <= scene_start
                || events.is_empty()
            {
                return None;
            }
            for event in events {
                let event_id = event.get("event_id")?.as_str()?;
                let event_start = event.get("start_seconds")?.as_f64()?;
                let event_end = event.get("end_seconds")?.as_f64()?;
                if event_id.is_empty()
                    || !seen_event_ids.insert(event_id)
                    || event_end <= event_start
                    || event_start < scene_start - EPSILON
                    || event_end > scene_end + EPSILON
                {
                    return None;
                }
            }
        }
        let cues = parse_srt(source_bytes.get("srt")?)?;
        let global_dependency_sha256 = canonical_digest(&serde_json::json!({
            "schema": "haru.segment_global_dependencies.v1",
            "editorial_contract_sha256": current_inputs.get("editorial_contract")?.get("sha256")?,
            "narration_sha256": current_inputs.get("narration")?.get("sha256")?,
            "storyboard_authority": storyboard_authority,
            "locality": locality_value(&plan, segments)?,
        }))?;

        let mut covered_scenes = Vec::new();
        let mut covered_events = Vec::new();
        let mut covered_cues = Vec::new();
        let mut previous_end = None;
        let mut authority = Vec::with_capacity(segments.len());
        for (index, segment) in segments.iter().enumerate() {
            let id = SEGMENT_IDS[index];
            let output = SEGMENT_OUTPUTS[index];
            if segment.get("segment_id")?.as_str()? != id
                || segment.get("ordinal")?.as_u64()? != (index + 1) as u64
                || segment.get("output")?.as_str()? != output
                || !segment
                    .get("narrative_role")?
                    .as_str()
                    .is_some_and(|v| !v.trim().is_empty())
            {
                return None;
            }
            let selector = required_object(
                segment.get("selector")?,
                &["kind", "fps", "start_frame", "end_frame"],
            )?;
            let fps = selector.get("fps")?.as_u64()?;
            let start_frame = selector.get("start_frame")?.as_u64()?;
            let end_frame = selector.get("end_frame")?.as_u64()?;
            if selector.get("kind")?.as_str()? != "frame_range.v1"
                || fps == 0
                || end_frame <= start_frame
            {
                return None;
            }

            let scene_ids = string_list(segment.get("scene_ids")?)?;
            let event_ids = string_list(segment.get("event_ids")?)?;
            let mut owned_scenes = Vec::new();
            let mut owned_events = Vec::new();
            for scene_id in &scene_ids {
                let scene = storyboard_scenes.iter().find(|scene| {
                    scene.get("scene_id").and_then(Value::as_str) == Some(*scene_id)
                })?;
                owned_scenes.push(scene.clone());
                let events = scene.get("visual_events")?.as_array()?;
                if events.is_empty() {
                    return None;
                }
                owned_events.extend(events.iter().cloned());
            }
            let actual_event_ids = owned_events
                .iter()
                .map(|event| event.get("event_id")?.as_str())
                .collect::<Option<Vec<_>>>()?;
            if actual_event_ids != event_ids {
                return None;
            }
            covered_scenes.extend(scene_ids.iter().copied());
            covered_events.extend(event_ids.iter().copied());

            let first_scene = owned_scenes.first()?;
            let last_scene = owned_scenes.last()?;
            let first_event = owned_events.first()?;
            let last_event = owned_events.last()?;
            let start = required_object(
                segment.get("start")?,
                &["scene_id", "event_id", "cue_index", "seconds"],
            )?;
            let end = required_object(
                segment.get("end")?,
                &["scene_id", "event_id", "cue_index", "seconds"],
            )?;
            let start_seconds = start.get("seconds")?.as_f64()?;
            let end_seconds = end.get("seconds")?.as_f64()?;
            let first_cue = cues.iter().find(|cue| close(cue.start, start_seconds))?;
            let last_cue = cues.iter().rev().find(|cue| close(cue.end, end_seconds))?;
            if start.get("scene_id")?.as_str()? != first_scene.get("scene_id")?.as_str()?
                || start.get("event_id")?.as_str()? != first_event.get("event_id")?.as_str()?
                || start.get("cue_index")?.as_u64()? != first_cue.index as u64
                || end.get("scene_id")?.as_str()? != last_scene.get("scene_id")?.as_str()?
                || end.get("event_id")?.as_str()? != last_event.get("event_id")?.as_str()?
                || end.get("cue_index")?.as_u64()? != last_cue.index as u64
                || !close(start_seconds, first_scene.get("start_seconds")?.as_f64()?)
                || !close(start_seconds, first_event.get("start_seconds")?.as_f64()?)
                || !close(end_seconds, last_scene.get("end_seconds")?.as_f64()?)
                || !close(end_seconds, last_event.get("end_seconds")?.as_f64()?)
                || end_seconds <= start_seconds
                || previous_end.is_some_and(|value| !close(value, start_seconds))
                || start_frame != frame_index(start_seconds, fps)?
                || end_frame != frame_index(end_seconds, fps)?
            {
                return None;
            }
            previous_end = Some(end_seconds);

            let owned_cues = cues
                .iter()
                .filter(|cue| {
                    cue.start >= start_seconds - EPSILON && cue.end <= end_seconds + EPSILON
                })
                .collect::<Vec<_>>();
            if owned_cues.is_empty()
                || cues.iter().any(|cue| {
                    cue.start < end_seconds - EPSILON
                        && cue.end > start_seconds + EPSILON
                        && !owned_cues.iter().any(|owned| owned.index == cue.index)
                })
            {
                return None;
            }
            covered_cues.extend(owned_cues.iter().map(|cue| cue.index));
            let cue_values = owned_cues
                .iter()
                .map(|cue| {
                    serde_json::json!({
                        "cue_index": cue.index,
                        "start_seconds": cue.start,
                        "end_seconds": cue.end,
                        "text": cue.text,
                    })
                })
                .collect::<Vec<_>>();
            let definition_sha256 = canonical_digest(segment)?;
            let dependency_sha256 = canonical_digest(&serde_json::json!({
                "schema": "haru.segment_dependencies.v1",
                "segment_id": id,
                "definition_sha256": definition_sha256,
                "global_dependency_sha256": global_dependency_sha256,
                "storyboard": {"scenes": owned_scenes, "events": owned_events},
                "srt_cues": cue_values,
            }))?;
            authority.push(SegmentBinding {
                id,
                output,
                definition_sha256,
                dependency_sha256,
                selector: segment.get("selector")?.clone(),
            });
        }

        let all_scene_ids = storyboard_scenes
            .iter()
            .map(|scene| scene.get("scene_id")?.as_str())
            .collect::<Option<Vec<_>>>()?;
        let all_event_ids = storyboard_scenes
            .iter()
            .flat_map(|scene| {
                scene
                    .get("visual_events")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
            })
            .map(|event| event.get("event_id")?.as_str())
            .collect::<Option<Vec<_>>>()?;
        if covered_scenes != all_scene_ids
            || covered_events != all_event_ids
            || covered_cues != cues.iter().map(|cue| cue.index).collect::<Vec<_>>()
        {
            return None;
        }
        Some(Self {
            segments: authority,
            assembly_policy_sha256,
        })
    }

    pub(crate) fn binding(&self, segment_id: &str) -> Option<(usize, &SegmentBinding)> {
        self.segments
            .iter()
            .enumerate()
            .find(|(_, segment)| segment.id == segment_id)
    }

    pub(crate) fn preceding_approvals_current(&self, project: &Path, index: usize) -> bool {
        self.segments[..index]
            .iter()
            .all(|segment| review_current(project, segment, None, Some("pass")))
    }

    pub(crate) fn all_approvals_current(&self, project: &Path) -> bool {
        self.segments
            .iter()
            .all(|segment| review_current(project, segment, None, Some("pass")))
    }

    pub(crate) fn assembly_current(
        &self,
        project: &Path,
        returned: Option<&Value>,
        mix_input_sha256: Option<&str>,
    ) -> bool {
        if !self.all_approvals_current(project) {
            return false;
        }
        let Some(returned) =
            returned.and_then(|value| required_object(value, &["schema", "path", "sha256"]))
        else {
            return false;
        };
        let Ok(artifact) = Artifact::from_path(project, &project.join(ASSEMBLY_RECEIPT)) else {
            return false;
        };
        if artifact.path != ASSEMBLY_RECEIPT
            || returned.get("schema").and_then(Value::as_str) != Some("haru.segment_assembly.v1")
            || returned.get("path").and_then(Value::as_str) != Some(ASSEMBLY_RECEIPT)
            || returned.get("sha256").and_then(Value::as_str) != Some(&artifact.sha256)
        {
            return false;
        }
        let Some(receipt) = read_json(&project.join(ASSEMBLY_RECEIPT)) else {
            return false;
        };
        let Some(receipt) = required_object(
            &receipt,
            &[
                "schema",
                "project",
                "output",
                "segments",
                "source_receipts",
                "transition_policy",
                "audio_policy",
                "policy_sha256",
                "method",
                "target_profile",
                "source_stream_profiles",
                "output_stream_profile",
                "output_sha256",
                "bytes",
                "duration_seconds",
                "decode_evidence",
            ],
        ) else {
            return false;
        };
        let Ok(premix) = Artifact::from_path(project, &project.join(ASSEMBLY_OUTPUT)) else {
            return false;
        };
        if receipt.get("schema").and_then(Value::as_str) != Some("haru.segment_assembly.v1")
            || receipt.get("project").and_then(Value::as_str)
                != project.file_name().and_then(|name| name.to_str())
            || receipt.get("output").and_then(Value::as_str) != Some(ASSEMBLY_OUTPUT)
            || receipt.get("transition_policy").and_then(Value::as_str) != Some("cut.v1")
            || receipt.get("audio_policy").and_then(Value::as_str) != Some("premix.v1")
            || receipt.get("policy_sha256").and_then(Value::as_str)
                != Some(&self.assembly_policy_sha256)
            || !matches!(
                receipt.get("method").and_then(Value::as_str),
                Some("ffmpeg_concat_copy" | "ffmpeg_deterministic_reencode")
            )
            || receipt.get("output_sha256").and_then(Value::as_str) != Some(&premix.sha256)
            || receipt.get("bytes").and_then(Value::as_u64) != Some(premix.bytes)
            || mix_input_sha256 != Some(&premix.sha256)
            || !receipt
                .get("duration_seconds")
                .and_then(Value::as_f64)
                .is_some_and(|value| value.is_finite() && value > 0.0)
            || !receipt.get("target_profile").is_some_and(Value::is_object)
            || !receipt
                .get("output_stream_profile")
                .is_some_and(Value::is_object)
        {
            return false;
        }
        let Some(decode) = receipt.get("decode_evidence").and_then(|value| {
            required_object(
                value,
                &[
                    "full_decode_clean",
                    "packet_dts_monotonic",
                    "frame_pts_monotonic",
                    "duration_within_frame_tolerance",
                    "frame_tolerance_seconds",
                ],
            )
        }) else {
            return false;
        };
        if [
            "full_decode_clean",
            "packet_dts_monotonic",
            "frame_pts_monotonic",
            "duration_within_frame_tolerance",
        ]
        .iter()
        .any(|key| decode.get(*key).and_then(Value::as_bool) != Some(true))
            || !decode
                .get("frame_tolerance_seconds")
                .and_then(Value::as_f64)
                .is_some_and(|value| value.is_finite() && value > 0.0)
        {
            return false;
        }
        let (Some(segments), Some(source_receipts), Some(profiles)) = (
            receipt.get("segments").and_then(Value::as_array),
            receipt.get("source_receipts").and_then(Value::as_array),
            receipt
                .get("source_stream_profiles")
                .and_then(Value::as_array),
        ) else {
            return false;
        };
        if segments.len() != self.segments.len()
            || source_receipts.len() != self.segments.len()
            || profiles.len() != self.segments.len()
        {
            return false;
        }
        let Some(stream_sets): Option<Vec<&Vec<Value>>> = profiles
            .iter()
            .map(|entry| {
                let profile = entry.get("profile")?;
                if !source_stream_profile_valid(profile) {
                    return None;
                }
                profile
                    .as_object()?
                    .get("streams")
                    .and_then(Value::as_array)
            })
            .collect()
        else {
            return false;
        };
        let compatible = stream_sets[1..]
            .iter()
            .all(|streams| *streams == stream_sets[0]);
        let expected_method = if compatible {
            "ffmpeg_concat_copy"
        } else {
            "ffmpeg_deterministic_reencode"
        };
        if receipt.get("method").and_then(Value::as_str) != Some(expected_method) {
            return false;
        }
        let Some(expected_target) = profiles[0].get("profile").and_then(expected_target_profile)
        else {
            return false;
        };
        if receipt.get("target_profile") != Some(&expected_target) {
            return false;
        }
        let Some(expected_tolerance) = target_frame_tolerance(&expected_target) else {
            return false;
        };
        if !decode
            .get("frame_tolerance_seconds")
            .and_then(Value::as_f64)
            .is_some_and(|value| close(value, expected_tolerance))
        {
            return false;
        }
        let Some(receipt_duration) = receipt.get("duration_seconds").and_then(Value::as_f64) else {
            return false;
        };
        let Some(output_profile) = receipt.get("output_stream_profile") else {
            return false;
        };
        if !output_stream_profile_valid(
            output_profile,
            &expected_target,
            receipt_duration,
            !compatible,
        ) {
            return false;
        }
        if compatible
            && output_profile
                .as_object()
                .and_then(|value| value.get("streams"))
                .and_then(Value::as_array)
                != Some(stream_sets[0])
        {
            return false;
        }
        self.segments.iter().enumerate().all(|(index, segment)| {
            let Some(tuple) = required_object(
                &segments[index],
                &[
                    "segment_id",
                    "ordinal",
                    "definition_sha256",
                    "dependency_sha256",
                    "video_sha256",
                    "bytes",
                    "duration_seconds",
                ],
            ) else {
                return false;
            };
            let Some(receipts) = required_object(
                &source_receipts[index],
                &[
                    "segment_id",
                    "render_sha256",
                    "evidence_sha256",
                    "review_sha256",
                    "stream_profile_sha256",
                ],
            ) else {
                return false;
            };
            let Some(profile) = required_object(&profiles[index], &["segment_id", "profile"])
            else {
                return false;
            };
            let render_relative = format!("quality-review/segments/{}/render.json", segment.id);
            let evidence_relative = format!("quality-review/segments/{}/evidence.json", segment.id);
            let review_relative = format!("quality-review/segments/{}/review.json", segment.id);
            let (Ok(video), Ok(render_artifact), Ok(evidence_artifact), Ok(review_artifact)) = (
                Artifact::from_path(project, &project.join(segment.output)),
                Artifact::from_path(project, &project.join(&render_relative)),
                Artifact::from_path(project, &project.join(&evidence_relative)),
                Artifact::from_path(project, &project.join(&review_relative)),
            ) else {
                return false;
            };
            let Some(render) = read_json(&project.join(&render_relative)) else {
                return false;
            };
            let Some(render_profile) = render.get("stream_profile") else {
                return false;
            };
            let Some(render_profile_sha256) = canonical_digest(render_profile) else {
                return false;
            };
            tuple.get("segment_id").and_then(Value::as_str) == Some(segment.id)
                && tuple.get("ordinal").and_then(Value::as_u64) == Some((index + 1) as u64)
                && tuple.get("definition_sha256").and_then(Value::as_str)
                    == Some(&segment.definition_sha256)
                && tuple.get("dependency_sha256").and_then(Value::as_str)
                    == Some(&segment.dependency_sha256)
                && tuple.get("video_sha256").and_then(Value::as_str) == Some(&video.sha256)
                && tuple.get("bytes").and_then(Value::as_u64) == Some(video.bytes)
                && tuple.get("duration_seconds") == render.get("duration_seconds")
                && receipts.get("segment_id").and_then(Value::as_str) == Some(segment.id)
                && receipts.get("render_sha256").and_then(Value::as_str)
                    == Some(&render_artifact.sha256)
                && receipts.get("evidence_sha256").and_then(Value::as_str)
                    == Some(&evidence_artifact.sha256)
                && receipts.get("review_sha256").and_then(Value::as_str)
                    == Some(&review_artifact.sha256)
                && receipts
                    .get("stream_profile_sha256")
                    .and_then(Value::as_str)
                    == Some(render_profile_sha256.as_str())
                && profile.get("segment_id").and_then(Value::as_str) == Some(segment.id)
                && profile.get("profile") == Some(render_profile)
                && review_current(project, segment, None, Some("pass"))
        })
    }

    pub(crate) fn assembly_receipt_current(
        &self,
        project: &Path,
        returned: Option<&Value>,
    ) -> bool {
        let Some(returned) = returned else {
            return false;
        };
        let Some(receipt) = read_json(&project.join(ASSEMBLY_RECEIPT)) else {
            return false;
        };
        if returned != &receipt {
            return false;
        }
        let Ok(artifact) = Artifact::from_path(project, &project.join(ASSEMBLY_RECEIPT)) else {
            return false;
        };
        let Some(output_sha256) = receipt.get("output_sha256").and_then(Value::as_str) else {
            return false;
        };
        let binding = serde_json::json!({
            "schema": "haru.segment_assembly.v1",
            "path": ASSEMBLY_RECEIPT,
            "sha256": artifact.sha256,
        });
        self.assembly_current(project, Some(&binding), Some(output_sha256))
    }
}

fn segment_paths(segment: &SegmentBinding) -> (String, String) {
    (
        format!("quality-review/segments/{}/render.json", segment.id),
        format!("quality-review/segments/{}/evidence.json", segment.id),
    )
}

pub(crate) fn render_current(
    project: &Path,
    segment: &SegmentBinding,
    returned: Option<&Value>,
) -> bool {
    let (render_relative, evidence_relative) = segment_paths(segment);
    let Some(render) = read_json(&project.join(&render_relative)) else {
        return false;
    };
    if returned.is_some_and(|value| value != &render) {
        return false;
    }
    let Some(evidence) = read_json(&project.join(&evidence_relative)) else {
        return false;
    };
    let Ok(video) = Artifact::from_path(project, &project.join(segment.output)) else {
        return false;
    };
    let Ok(render_artifact) = Artifact::from_path(project, &project.join(&render_relative)) else {
        return false;
    };
    let Ok(evidence_artifact) = Artifact::from_path(project, &project.join(&evidence_relative))
    else {
        return false;
    };
    let required_samples = [
        ("head", "head.jpg"),
        ("tail", "tail.jpg"),
        ("authored_transition", "authored-transition.jpg"),
        ("caption_window", "caption-window.jpg"),
        ("waveform_window", "waveform.png"),
    ];
    let samples_current = evidence
        .get("samples")
        .and_then(Value::as_array)
        .is_some_and(|samples| {
            samples.len() == required_samples.len()
                && required_samples.iter().all(|(kind, name)| {
                    let relative = format!("quality-review/segments/{}/{name}", segment.id);
                    samples
                        .iter()
                        .find(|sample| sample.get("kind").and_then(Value::as_str) == Some(*kind))
                        .is_some_and(|sample| {
                            sample.get("path").and_then(Value::as_str) == Some(relative.as_str())
                                && sample
                                    .get("seconds")
                                    .and_then(Value::as_f64)
                                    .is_some_and(|value| value >= 0.0)
                                && Artifact::from_path(project, &project.join(&relative)).is_ok_and(
                                    |artifact| {
                                        artifact.path == relative
                                            && sample.get("sha256").and_then(Value::as_str)
                                                == Some(&artifact.sha256)
                                            && sample.get("bytes").and_then(Value::as_u64)
                                                == Some(artifact.bytes)
                                    },
                                )
                        })
                })
        });
    render.get("schema").and_then(Value::as_str) == Some("haru.segment_render.v1")
        && render.get("status").and_then(Value::as_str) == Some("render_complete")
        && render.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && render.get("segment_id").and_then(Value::as_str) == Some(segment.id)
        && render.get("output").and_then(Value::as_str) == Some(segment.output)
        && render.get("definition_sha256").and_then(Value::as_str)
            == Some(&segment.definition_sha256)
        && render.get("dependency_sha256").and_then(Value::as_str)
            == Some(&segment.dependency_sha256)
        && render.get("selector") == Some(&segment.selector)
        && render.get("decode_clean").and_then(Value::as_bool) == Some(true)
        && render
            .get("duration_seconds")
            .and_then(Value::as_f64)
            .is_some_and(|value| value.is_finite() && value > 0.0)
        && render
            .get("stream_profile")
            .is_some_and(source_stream_profile_valid)
        && render.get("video_sha256").and_then(Value::as_str) == Some(&video.sha256)
        && render.get("bytes").and_then(Value::as_u64) == Some(video.bytes)
        && render
            .get("evidence")
            .and_then(|value| value.get("path"))
            .and_then(Value::as_str)
            == Some(evidence_relative.as_str())
        && render
            .get("evidence")
            .and_then(|value| value.get("sha256"))
            .and_then(Value::as_str)
            == Some(&evidence_artifact.sha256)
        && video.path == segment.output
        && evidence_artifact.path == evidence_relative
        && render_artifact.path == render_relative
        && evidence.get("schema").and_then(Value::as_str) == Some("haru.segment_review_evidence.v1")
        && evidence.get("segment_id").and_then(Value::as_str) == Some(segment.id)
        && evidence.get("video").and_then(Value::as_str) == Some(segment.output)
        && evidence.get("video_sha256") == render.get("video_sha256")
        && evidence.get("definition_sha256") == render.get("definition_sha256")
        && evidence.get("dependency_sha256") == render.get("dependency_sha256")
        && samples_current
}

fn valid_reviewed_at(value: Option<&Value>) -> bool {
    let Some(value) = value.and_then(Value::as_str) else {
        return false;
    };
    let bytes = value.as_bytes();
    if bytes.len() != 25
        || bytes.get(4) != Some(&b'-')
        || bytes.get(7) != Some(&b'-')
        || bytes.get(10) != Some(&b'T')
        || bytes.get(13) != Some(&b':')
        || bytes.get(16) != Some(&b':')
        || bytes.get(19..25) != Some(b"+00:00")
    {
        return false;
    }
    let parse = |start: usize, length: usize| {
        std::str::from_utf8(bytes.get(start..start + length)?)
            .ok()?
            .parse::<u32>()
            .ok()
    };
    let (Some(year), Some(month), Some(day), Some(hour), Some(minute), Some(second)) = (
        parse(0, 4),
        parse(5, 2),
        parse(8, 2),
        parse(11, 2),
        parse(14, 2),
        parse(17, 2),
    ) else {
        return false;
    };
    let leap = year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        _ => return false,
    };
    day > 0 && day <= days && hour < 24 && minute < 60 && second < 60
}

pub(crate) fn review_current(
    project: &Path,
    segment: &SegmentBinding,
    returned: Option<&Value>,
    required_verdict: Option<&str>,
) -> bool {
    let review_relative = format!("quality-review/segments/{}/review.json", segment.id);
    let Some(review) = read_json(&project.join(&review_relative)) else {
        return false;
    };
    if returned.is_some_and(|value| value != &review)
        || required_verdict
            .is_some_and(|verdict| review.get("verdict").and_then(Value::as_str) != Some(verdict))
        || !render_current(project, segment, None)
    {
        return false;
    }
    let (_, evidence_relative) = segment_paths(segment);
    let Ok(video) = Artifact::from_path(project, &project.join(segment.output)) else {
        return false;
    };
    let Ok(evidence) = Artifact::from_path(project, &project.join(&evidence_relative)) else {
        return false;
    };
    let Ok(review_artifact) = Artifact::from_path(project, &project.join(&review_relative)) else {
        return false;
    };
    review.get("schema").and_then(Value::as_str) == Some("haru.segment_review.v1")
        && review.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && review.get("segment_id").and_then(Value::as_str) == Some(segment.id)
        && review.get("output").and_then(Value::as_str) == Some(segment.output)
        && video.path == segment.output
        && evidence.path == evidence_relative
        && review_artifact.path == review_relative
        && review.get("video_sha256").and_then(Value::as_str) == Some(&video.sha256)
        && review.get("definition_sha256").and_then(Value::as_str)
            == Some(&segment.definition_sha256)
        && review.get("dependency_sha256").and_then(Value::as_str)
            == Some(&segment.dependency_sha256)
        && review.get("evidence_sha256").and_then(Value::as_str) == Some(&evidence.sha256)
        && matches!(
            review.get("verdict").and_then(Value::as_str),
            Some("pass" | "changes_requested")
        )
        && review
            .get("reviewed_by")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.trim().is_empty())
        && valid_reviewed_at(review.get("reviewed_at"))
        && review
            .get("notes")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.trim().is_empty())
}

#[cfg(test)]
mod tests {
    use super::{canonical_digest, frame_index};

    #[test]
    fn canonical_digest_matches_the_cross_runtime_golden_vector() {
        let value: serde_json::Value = serde_json::from_str(
            r#"{"z":[null,true,false,-1,9223372036854775808,0.00001,"雪"],"a":{"opacity":1e-5,"half":0.15},"overflow":18446744073709551616}"#,
        )
        .unwrap();
        assert_eq!(
            canonical_digest(&value).as_deref(),
            Some("12a96cd79d958826256e03a27b661315dc460d46913e67e2607754f020dc2cb8")
        );
    }

    #[test]
    fn frame_index_uses_ties_to_even() {
        assert_eq!(frame_index(0.15, 30), Some(4));
        assert_eq!(frame_index(0.25, 30), Some(8));
    }
}
