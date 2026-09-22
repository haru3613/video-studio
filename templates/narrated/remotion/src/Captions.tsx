import React from "react";
import type {Caption} from "@remotion/captions";
import {
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import type {NarratedContent} from "./schema";

type CaptionsProps = {
  captions: Caption[];
  format: "landscape" | "portrait";
  palette: NarratedContent["palette"];
};

export const Captions: React.FC<CaptionsProps> = ({
  captions,
  format,
  palette,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const currentMs = (frame / fps) * 1000;
  const caption =
    captions.find(
      (candidate) =>
        candidate.startMs <= currentMs && currentMs < candidate.endMs,
    );
  if (!caption) return null;
  const localFrame = frame - (caption.startMs / 1000) * fps;

  return (
    <div
      style={{
        position: "absolute",
        left: format === "portrait" ? 70 : 120,
        right: format === "portrait" ? 70 : 120,
        bottom: format === "portrait" ? 150 : 82,
        display: "flex",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          maxWidth: format === "portrait" ? 900 : 1500,
          padding: format === "portrait" ? "26px 34px" : "20px 34px",
          borderRadius: 24,
          backgroundColor: `${palette.background}E8`,
          border: `1px solid ${palette.muted}45`,
          color: palette.ink,
          fontSize: format === "portrait" ? 58 : 48,
          fontWeight: 750,
          lineHeight: 1.16,
          textAlign: "center",
          opacity: interpolate(localFrame, [0, fps * 0.3], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
          translate: `0 ${interpolate(localFrame, [0, fps * 0.3], [16, 0], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          })}px`,
        }}
      >
        {caption.text}
      </div>
    </div>
  );
};
