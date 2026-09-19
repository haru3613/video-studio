import React from "react";
import {AbsoluteFill} from "remotion";
import type {NarratedContent} from "./schema";

export const CoverStill: React.FC<{content: NarratedContent}> = ({content}) => {
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
          backgroundImage:
            `radial-gradient(circle at 76% 45%, ${content.palette.accent}2D, transparent 35%), linear-gradient(135deg, transparent 0 54%, ${content.palette.surface} 54% 100%)`,
        }}
      />
      <div
        style={{
          position: "absolute",
          left: 84,
          top: 74,
          color: content.palette.accent,
          fontSize: 28,
          fontWeight: 800,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
        }}
      >
        {content.kicker}
      </div>
      <div
        style={{
          position: "absolute",
          left: 80,
          top: 180,
          width: 760,
          fontSize: 94,
          fontWeight: 850,
          lineHeight: 0.98,
          letterSpacing: "-0.04em",
        }}
      >
        {content.title}
      </div>
      <svg
        width="430"
        height="430"
        viewBox="0 0 430 430"
        style={{position: "absolute", right: 70, top: 145}}
      >
        <circle cx="215" cy="215" r="168" fill="none" stroke={content.palette.accent} strokeWidth="4" opacity="0.25" />
        <circle cx="215" cy="215" r="112" fill="none" stroke={content.palette.accent} strokeWidth="7" opacity="0.45" />
        <circle cx="215" cy="215" r="48" fill={content.palette.accent} />
        <path d="M215 167 L215 75 M263 215 L355 215 M215 263 L215 355" stroke={content.palette.accentWarm} strokeWidth="10" strokeLinecap="round" />
        <circle cx="215" cy="58" r="18" fill={content.palette.accentWarm} />
        <circle cx="372" cy="215" r="18" fill={content.palette.accentWarm} />
        <circle cx="215" cy="372" r="18" fill={content.palette.accentWarm} />
      </svg>
      <div
        style={{
          position: "absolute",
          left: 86,
          bottom: 66,
          color: content.palette.muted,
          fontSize: 24,
          fontWeight: 650,
        }}
      >
        Cue-driven local video template
      </div>
    </AbsoluteFill>
  );
};
