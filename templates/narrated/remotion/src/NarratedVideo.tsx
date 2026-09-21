import React from "react";
import {Audio} from "@remotion/media";
import {
  AbsoluteFill,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {Captions} from "./Captions";
import {SceneVisual} from "./Visuals";
import type {NarratedContent} from "./schema";
import {frameAtMs} from "./schema";

export type NarratedVideoProps = {
  content: NarratedContent;
  format: "landscape" | "portrait";
};

export const NarratedVideo: React.FC<NarratedVideoProps> = ({
  content,
  format,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const currentMs = (frame / fps) * 1000;
  const scene =
    content.scenes.find(
      (candidate) =>
        candidate.startMs <= currentMs && currentMs < candidate.endMs,
    ) ?? content.scenes[content.scenes.length - 1];
  const event =
    scene.events.find(
      (candidate) =>
        candidate.startMs <= currentMs && currentMs < candidate.endMs,
    ) ?? scene.events[scene.events.length - 1];

  return (
    <AbsoluteFill
      style={{
        backgroundColor: content.palette.background,
        color: content.palette.ink,
        fontFamily:
          "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
        overflow: "hidden",
      }}
    >
      <AbsoluteFill
        style={{
          opacity: 0.34,
          backgroundImage:
            `linear-gradient(${content.palette.muted}12 1px, transparent 1px), linear-gradient(90deg, ${content.palette.muted}12 1px, transparent 1px)`,
          backgroundSize: format === "portrait" ? "72px 72px" : "88px 88px",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: format === "portrait" ? 100 : 70,
          left: format === "portrait" ? 76 : 100,
          right: format === "portrait" ? 76 : 100,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          color: content.palette.muted,
          fontSize: format === "portrait" ? 28 : 24,
          fontWeight: 700,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
        }}
      >
        <span>{content.kicker}</span>
        <span>{String(Math.floor(currentMs / 1000) + 1).padStart(2, "0")}</span>
      </div>

      <SceneVisual
        event={event}
        sceneLabel={scene.label}
        sceneStartMs={scene.startMs}
        format={format}
        palette={content.palette}
      />
      <Captions
        captions={content.captions}
        format={format}
        palette={content.palette}
      />

      {content.media.narration ? (
        <Audio src={staticFile(content.media.narration.path)} />
      ) : null}
      {content.media.backgroundMusic ? (
        <Audio
          src={staticFile(content.media.backgroundMusic.path)}
          loop
          volume={content.media.backgroundMusic.volume ?? 0.12}
        />
      ) : null}
      {content.media.soundEffects.map((effect) => (
        <Sequence
          key={`${effect.path}-${effect.startMs}`}
          from={frameAtMs(effect.startMs, fps)}
          layout="none"
        >
          <Audio src={staticFile(effect.path)} volume={effect.volume} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
