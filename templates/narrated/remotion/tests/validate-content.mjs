import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {resolve} from "node:path";

function assertLocalPath(path) {
  assert.equal(typeof path, "string");
  assert.ok(path && !path.startsWith("/") && !path.includes("..") && !path.includes("\\") && !/^[a-z][a-z0-9+.-]*:/i.test(path));
}

const file = resolve(process.argv[2] ?? "src/content.json");
const content = JSON.parse(readFileSync(file, "utf8"));

assert.equal(content.schema, "video_studio.narrated_content.v1");
assert.equal(content.visual_timeline_contract, "cue_driven.v1");
assert.ok(Number.isInteger(content.fps) && content.fps > 0);
assert.ok(Number.isFinite(content.durationMs) && content.durationMs > 0);
assert.ok(Array.isArray(content.scenes) && content.scenes.length > 0);
assert.ok(Array.isArray(content.captions) && content.captions.length > 0);

let captionCursor = 0;
const cues = new Set();
for (const caption of content.captions) {
  assert.ok(Number.isFinite(caption.startMs) && Number.isFinite(caption.endMs));
  assert.ok(caption.startMs >= captionCursor, "captions must not overlap");
  assert.ok(caption.endMs <= content.durationMs);
  assert.ok(caption.endMs > caption.startMs);
  assert.equal(typeof caption.text, "string");
  assert.ok(caption.text.trim());
  assert.equal(caption.timestampMs, null);
  assert.equal(caption.confidence, null);
  captionCursor = caption.endMs;
  cues.add(caption.text);
}


const forbiddenTimers = new Set([
  "cadence_seconds",
  "focus_period_seconds",
  "interval_seconds",
  "period_seconds",
  "rotate_every_seconds",
  "timer_seconds",
]);
let sceneCursor = 0;
const eventIds = new Set();
for (const scene of content.scenes) {
  assert.equal(scene.startMs, sceneCursor, "scenes must be contiguous");
  assert.ok(scene.endMs > scene.startMs);
  assert.ok(Array.isArray(scene.events) && scene.events.length > 0);
  let eventCursor = scene.startMs;
  for (const event of scene.events) {
    assert.equal(event.startMs, eventCursor, "events must be contiguous");
    assert.ok(event.endMs > event.startMs);
    assert.ok(event.endMs <= scene.endMs);
    assert.ok(cues.has(event.cue), "event cue must equal a caption");
    assert.ok(!eventIds.has(event.eventId), "event IDs must be unique");
    eventIds.add(event.eventId);
    for (const key of Object.keys(event)) {
      assert.ok(!forbiddenTimers.has(key), `semantic timer is forbidden: ${key}`);
    }
    if (event.visual) {
      assert.ok(["signal", "cards", "steps", "image", "video"].includes(event.visual.kind));
      if (["image", "video"].includes(event.visual.kind)) assertLocalPath(event.visual.path);
      if (event.visual.labels) {
        assert.equal(event.visual.labels.length, 3);
        assert.ok(event.visual.labels.every((label) => typeof label === "string" && label.trim() && label.length <= 60));
      }
      if (event.visual.fit) assert.ok(["contain", "cover"].includes(event.visual.fit));
    }
    eventCursor = event.endMs;
  }
  assert.equal(eventCursor, scene.endMs);
  sceneCursor = scene.endMs;
}
assert.equal(sceneCursor, content.durationMs);

const tracks = [
  content.media.narration,
  content.media.backgroundMusic,
  ...content.media.soundEffects,
].filter(Boolean);
for (const track of tracks) {
  assert.equal(typeof track.path, "string");
  assert.ok(!track.path.startsWith("/"));
  assert.ok(!track.path.includes(".."));
  assert.ok(!/^[a-z][a-z0-9+.-]*:/i.test(track.path));
}
if (content.media.narration?.kind === "synthetic_tone_not_speech") {
  assert.match(content.media.narration.label, /not narration|not speech/i);
}

console.log(
  JSON.stringify({
    ok: true,
    file,
    durationMs: content.durationMs,
    scenes: content.scenes.length,
    events: eventIds.size,
    captions: content.captions.length,
  }),
);
