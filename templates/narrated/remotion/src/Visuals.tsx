import React from "react";
import {
  Easing,
  Img,
  OffthreadVideo,
  Sequence,
  staticFile,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import type {NarratedContent, VisualEvent} from "./schema";

type VisualProps = {
  event: VisualEvent;
  sceneLabel: string;
  sceneStartMs: number;
  format: "landscape" | "portrait";
  palette: NarratedContent["palette"];
};

const clamp = {
  extrapolateLeft: "clamp" as const,
  extrapolateRight: "clamp" as const,
};

const SignalVisual: React.FC<VisualProps> = ({
  event,
  format,
  palette,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const localFrame = frame - (event.startMs / 1000) * fps;
  const reveal = interpolate(localFrame, [0, fps * 0.8], [0, 1], {
    ...clamp,
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });
  const radius = interpolate(localFrame, [0, fps * 2.5], [54, 150], clamp);
  const isContext = event.visualState === "signal.context";
  const size = format === "portrait" ? 700 : 760;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 760 760"
      style={{maxWidth: "76%", maxHeight: "68%"}}
    >
      <circle cx="380" cy="380" r={radius} fill="none" stroke={palette.accent} strokeWidth="3" opacity={0.18} />
      <circle cx="380" cy="380" r={radius * 0.68} fill="none" stroke={palette.accent} strokeWidth="5" opacity={0.28} />
      <circle cx="380" cy="380" r="42" fill={palette.accent} opacity={reveal} />
      <circle cx="380" cy="380" r="13" fill={palette.ink} />
      {isContext ? (
        <>
          <path d="M380 338 L380 160" stroke={palette.accentWarm} strokeWidth="7" strokeLinecap="round" opacity={reveal} />
          <path d="M422 380 L600 380" stroke={palette.accentWarm} strokeWidth="7" strokeLinecap="round" opacity={reveal} />
          <path d="M380 422 L380 600" stroke={palette.accentWarm} strokeWidth="7" strokeLinecap="round" opacity={reveal} />
          <circle cx="380" cy="145" r="18" fill={palette.accentWarm} opacity={reveal} />
          <circle cx="615" cy="380" r="18" fill={palette.accentWarm} opacity={reveal} />
          <circle cx="380" cy="615" r="18" fill={palette.accentWarm} opacity={reveal} />
        </>
      ) : null}
    </svg>
  );
};

const FilterVisual: React.FC<VisualProps> = ({
  event,
  format,
  palette,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const localFrame = frame - (event.startMs / 1000) * fps;
  const progress = interpolate(localFrame, [0, fps * 1.1], [0, 1], {
    ...clamp,
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });
  const baseline = event.visualState === "filter.baseline";
  const vertical = format === "portrait";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: vertical ? "column" : "row",
        gap: vertical ? 30 : 36,
        alignItems: "center",
        justifyContent: "center",
        width: "82%",
      }}
    >
      {(event.visual?.labels ?? ["Noise", "Pattern", "Baseline"]).map((label, index) => {
        const active = index === (event.visual?.activeIndex ?? (baseline ? 2 : 1));
        return (
          <div
            key={index}
            style={{
              width: vertical ? 600 : 320,
              height: vertical ? 210 : 340,
              borderRadius: 28,
              border: `2px solid ${active ? palette.accent : palette.muted}55`,
              backgroundColor: active ? `${palette.accent}20` : palette.surface,
              display: "flex",
              flexDirection: vertical ? "row" : "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 22,
              opacity: interpolate(progress, [0, 1], [0.3, index <= 1 || baseline ? 1 : 0.55], clamp),
              translate: `0 ${interpolate(progress, [0, 1], [28, 0], clamp)}px`,
            }}
          >
            <div
              style={{
                width: active ? 88 : 56,
                height: active ? 88 : 56,
                borderRadius: 999,
                backgroundColor: active ? palette.accent : `${palette.muted}55`,
              }}
            />
            <span style={{color: active ? palette.ink : palette.muted, fontSize: 38, fontWeight: 700}}>
              {label}
            </span>
          </div>
        );
      })}
    </div>
  );
};

const DecisionVisual: React.FC<VisualProps> = ({
  event,
  format,
  palette,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const localFrame = frame - (event.startMs / 1000) * fps;
  const progress = interpolate(localFrame, [0, fps * 1.4], [0, 1], {
    ...clamp,
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });
  const result = event.visual?.activeIndex === 2 || event.visualState === "decision.result";
  const labels = event.visual?.labels ?? ["signal", "choice", "action"];
  const width = format === "portrait" ? 760 : 1050;
  const height = format === "portrait" ? 820 : 540;

  return (
    <svg width={width} height={height} viewBox="0 0 1050 540" style={{maxWidth: "84%"}}>
      <path
        d="M120 270 C310 270 320 125 515 125 C705 125 710 270 930 270"
        fill="none"
        stroke={palette.muted}
        strokeWidth="9"
        strokeLinecap="round"
        pathLength="1"
        strokeDasharray="1"
        strokeDashoffset={1 - progress}
      />
      <circle cx="120" cy="270" r="42" fill={palette.accent} />
      <circle cx="515" cy="125" r="42" fill={result ? palette.accentWarm : palette.surface} stroke={palette.accentWarm} strokeWidth="8" />
      <circle cx="930" cy="270" r="56" fill={result ? palette.accent : palette.surface} stroke={palette.accent} strokeWidth="9" />
      <path d="M902 269 l20 20 38-48" fill="none" stroke={palette.ink} strokeWidth="10" strokeLinecap="round" strokeLinejoin="round" opacity={result ? progress : 0.2} />
      <text x="120" y="360" textAnchor="middle" fill={palette.muted} fontSize="34">{labels[0]}</text>
      <text x="515" y="70" textAnchor="middle" fill={palette.muted} fontSize="34">{labels[1]}</text>
      <text x="930" y="370" textAnchor="middle" fill={palette.ink} fontSize="38" fontWeight="700">{labels[2]}</text>
    </svg>
  );
};

export const SceneVisual: React.FC<VisualProps> = (props) => {
  const {event, sceneLabel, format, palette} = props;
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const localFrame = frame - (event.startMs / 1000) * fps;

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: format === "portrait" ? 58 : 36,
        padding: format === "portrait" ? "170px 80px 330px" : "120px 120px 210px",
      }}
    >
      <div
        style={{
          color: palette.accent,
          fontSize: format === "portrait" ? 34 : 30,
          fontWeight: 800,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          opacity: interpolate(localFrame, [0, fps * 0.45], [0, 1], {
            ...clamp,
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
        }}
      >
        {sceneLabel}
      </div>
      {(event.visual?.kind === "signal" || event.visualState.startsWith("signal.")) ? <SignalVisual {...props} /> : null}
      {(event.visual?.kind === "cards" || event.visualState.startsWith("filter.")) ? <FilterVisual {...props} /> : null}
      {(event.visual?.kind === "steps" || event.visualState.startsWith("decision.")) ? <DecisionVisual {...props} /> : null}
      {event.visual?.kind === "image" && event.visual.path ? (
        <Img src={staticFile(event.visual.path)} style={{width: "90%", height: "65%", objectFit: event.visual.fit ?? "contain", borderRadius: 20}} />
      ) : null}
      {event.visual?.kind === "video" && event.visual.path ? (
        <Sequence from={Math.round(props.sceneStartMs / 1000 * fps)} layout="none">
          <OffthreadVideo muted src={staticFile(event.visual.path)} style={{width: "90%", height: "65%", objectFit: event.visual.fit ?? "contain", borderRadius: 20}} />
        </Sequence>
      ) : null}
      {event.visual?.text ? <div style={{fontSize: format === "portrait" ? 38 : 34, maxWidth: "85%", textAlign: "center", lineHeight: 1.4}}>{event.visual.text}</div> : null}
    </div>
  );
};
