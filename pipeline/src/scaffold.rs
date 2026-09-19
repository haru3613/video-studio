use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde_json::Value;
use uuid::Uuid;

use crate::application::SCAFFOLD_MARKER;

const TEXT_ARTIFACTS: &[(&str, &str, &str)] = &[
    (
        "script-proposal.md",
        "proposal",
        "The approved script. Written after topic selection.",
    ),
    (
        "sources.md",
        "sources",
        "Human-readable source list backing every factual claim.",
    ),
    (
        "narration-final.srt",
        "tts",
        "Subtitles for the final narration.",
    ),
    (
        "issue_brief.md",
        "duration",
        "Carries the duration target the render is measured against.",
    ),
];

const JSON_ARTIFACTS: &[(&str, &str, &str)] = &[
    (
        "claims.json",
        "sources",
        "Machine-checkable claims with source identity and URL.",
    ),
    (
        "narration-final.mp3.pron-ok.json",
        "tts",
        "Digest-bound pronunciation review.",
    ),
    (
        "storyboard-final-timed.json",
        "storyboard",
        "Timed scene and visual structure.",
    ),
    (
        "storyboard-final-timed-validation.json",
        "storyboard",
        "Timed storyboard validation.",
    ),
    (
        "editorial-contract.json",
        "editorial",
        "Cue-complete editorial shot contract.",
    ),
    (
        "quality-review/editorial-preview/review.json",
        "editorial_preview",
        "Digest-bound editorial preview verdict.",
    ),
    (
        "publish-metadata.json",
        "publish_pack",
        "Project-owned YouTube publish metadata.",
    ),
];

pub(crate) fn create_project(
    projects_root: &Path,
    slug: &str,
    runtime_contract: Value,
) -> io::Result<(PathBuf, Vec<String>)> {
    let project = projects_root.join(slug);
    if project.exists() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "project already exists",
        ));
    }

    let stage = projects_root.join(format!(".{slug}.{}.tmp", Uuid::new_v4()));
    fs::create_dir(&stage)?;
    let result = (|| {
        let mut written = Vec::new();
        write_json(
            &stage.join("project-contract.json"),
            &serde_json::json!({
                "schema": "haru.project_contract.v1",
                "runtime_contract": runtime_contract,
                "lane_contract": SCAFFOLD_MARKER,
                "production_profile": SCAFFOLD_MARKER,
            }),
        )?;
        written.push("project-contract.json".to_owned());

        for (path, gate, purpose) in TEXT_ARTIFACTS {
            write_file(
                &stage.join(path),
                format!("{SCAFFOLD_MARKER}\n\ngate: {gate}\npurpose: {purpose}\n").as_bytes(),
            )?;
            written.push((*path).to_owned());
        }
        for (path, gate, purpose) in JSON_ARTIFACTS {
            write_json(
                &stage.join(path),
                &serde_json::json!({"_todo": SCAFFOLD_MARKER, "_purpose": purpose, "_gate": gate}),
            )?;
            written.push((*path).to_owned());
        }

        write_file(&stage.join("narration-final.mp3"), b"")?;
        written.push("narration-final.mp3".to_owned());
        fs::create_dir_all(stage.join("output"))?;
        written.push("output/".to_owned());
        fs::create_dir_all(stage.join("quality-review"))?;
        written.push("quality-review/".to_owned());
        File::open(&stage)?.sync_all()?;
        fs::rename(&stage, &project)?;
        File::open(projects_root)?.sync_all()?;
        Ok((project, written))
    })();

    if result.is_err() {
        let _ = fs::remove_dir_all(&stage);
    }
    result
}

fn write_json(path: &Path, value: &Value) -> io::Result<()> {
    let mut bytes = serde_json::to_vec_pretty(value).map_err(io::Error::other)?;
    bytes.push(b'\n');
    write_file(path, &bytes)
}

fn write_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "artifact has no parent"))?;
    fs::create_dir_all(parent)?;
    let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()
}
