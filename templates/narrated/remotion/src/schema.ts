import type {Caption} from "@remotion/captions";

export type PresenterState = "hidden" | "talking" | "listening" | "reaction";

export type VisualEvent = {
  eventId: string;
  cue: string;
  visualState: string;
  presenterState: PresenterState;
  startMs: number;
  endMs: number;
  visual?: {
    kind: "signal" | "cards" | "steps" | "image" | "video";
    path?: string;
    fit?: "contain" | "cover";
    labels?: [string, string, string];
    activeIndex?: number;
    text?: string;
  };
};

export type Scene = {
  sceneId: string;
  label: string;
  startMs: number;
  endMs: number;
  events: VisualEvent[];
};

export type MediaTrack = {
  path: string;
  kind: "user_supplied_narration" | "synthetic_tone_not_speech" | "local_audio";
  label: string;
  volume?: number;
};

export type SoundEffect = MediaTrack & {
  startMs: number;
  volume: number;
};

export type NarratedContent = {
  schema: "video_studio.narrated_content.v1";
  visual_timeline_contract: "cue_driven.v1";
  title: string;
  kicker: string;
  fps: number;
  durationMs: number;
  palette: {
    background: string;
    surface: string;
    ink: string;
    muted: string;
    accent: string;
    accentWarm: string;
  };
  media: {
    narration: MediaTrack | null;
    backgroundMusic: MediaTrack | null;
    soundEffects: SoundEffect[];
  };
  captions: Caption[];
  scenes: Scene[];
};

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

const assertLocalPath = (value: string) => {
  if (
    !value || value.includes("\\") ||
    value.startsWith("/") ||
    value.includes("..") ||
    /^[a-z][a-z0-9+.-]*:/i.test(value)
  ) {
    throw new Error(`media path must be local to public/: ${value}`);
  }
};

export const validateContent = (value: NarratedContent): NarratedContent => {
  if (
    value.schema !== "video_studio.narrated_content.v1" ||
    value.visual_timeline_contract !== "cue_driven.v1"
  ) {
    throw new Error("unsupported narrated content schema");
  }
  if (
    !Number.isInteger(value.fps) ||
    value.fps <= 0 ||
    !isFiniteNumber(value.durationMs) ||
    value.durationMs <= 0
  ) {
    throw new Error("fps and durationMs must be positive");
  }
  if (!value.scenes.length || !value.captions.length) {
    throw new Error("at least one scene and caption is required");
  }

  let sceneCursor = 0;
  const cueSet = new Set(value.captions.map((caption) => caption.text));
  for (const scene of value.scenes) {
    if (
      scene.startMs !== sceneCursor ||
      scene.endMs <= scene.startMs ||
      !scene.events.length
    ) {
      throw new Error(`scene timeline is not contiguous at ${scene.sceneId}`);
    }
    let eventCursor = scene.startMs;
    for (const event of scene.events) {
      if (
        event.startMs !== eventCursor ||
        event.endMs <= event.startMs ||
        event.endMs > scene.endMs ||
        !cueSet.has(event.cue)
      ) {
        throw new Error(`event is not cue-locked: ${event.eventId}`);
      }
      if (event.visual) {
        const visual = event.visual;
        if (!["signal", "cards", "steps", "image", "video"].includes(visual.kind)) {
          throw new Error("unsupported visual kind");
        }
        if (visual.kind === "image" || visual.kind === "video") {
          assertLocalPath(visual.path ?? "");
          if (visual.fit && !["contain", "cover"].includes(visual.fit)) throw new Error("invalid media fit");
        }
        if (visual.labels && (visual.labels.length !== 3 || visual.labels.some((label) => typeof label !== "string" || !label.trim() || label.length > 60))) throw new Error("visual needs three bounded labels");
        if (visual.activeIndex !== undefined && (!Number.isInteger(visual.activeIndex) || visual.activeIndex < 0 || visual.activeIndex > 2)) throw new Error("invalid active index");
      }
      eventCursor = event.endMs;
    }
    if (eventCursor !== scene.endMs) {
      throw new Error(`events do not cover scene: ${scene.sceneId}`);
    }
    sceneCursor = scene.endMs;
  }
  if (sceneCursor !== value.durationMs) {
    throw new Error("scenes do not cover durationMs");
  }

  let captionCursor = 0;
  for (const caption of value.captions) {
    if (
      !isFiniteNumber(caption.startMs) || !isFiniteNumber(caption.endMs) ||
      caption.startMs < captionCursor ||
      caption.endMs > value.durationMs ||
      caption.endMs <= caption.startMs ||
      !caption.text.trim()
    ) {
      throw new Error(`caption timeline overlaps or exceeds duration at ${caption.startMs}`);
    }
    captionCursor = caption.endMs;
  }

  for (const track of [
    value.media.narration,
    value.media.backgroundMusic,
    ...value.media.soundEffects,
  ]) {
    if (track) {
      assertLocalPath(track.path);
      if (track.volume !== undefined && (!isFiniteNumber(track.volume) || track.volume < 0 || track.volume > 1)) throw new Error("invalid track volume");
    }
  }
  return value;
};

export const frameAtMs = (milliseconds: number, fps: number) =>
  Math.round((milliseconds / 1000) * fps);
