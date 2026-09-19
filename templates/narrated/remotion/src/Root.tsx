import React from "react";
import {Composition, Folder} from "remotion";
import rawContent from "./content.json";
import {CoverStill} from "./CoverStill";
import {NarratedVideo} from "./NarratedVideo";
import {
  type NarratedContent,
  frameAtMs,
  validateContent,
} from "./schema";

const content = validateContent(rawContent as NarratedContent);
const durationInFrames = frameAtMs(content.durationMs, content.fps);

export const VideoRoot: React.FC = () => {
  return (
    <>
      <Folder name="Narrated">
        <Composition
          id="NarratedLandscape"
          component={NarratedVideo}
          durationInFrames={durationInFrames}
          fps={content.fps}
          width={1920}
          height={1080}
          defaultProps={{content, format: "landscape" as const}}
        />
        <Composition
          id="NarratedPortrait"
          component={NarratedVideo}
          durationInFrames={durationInFrames}
          fps={content.fps}
          width={1080}
          height={1920}
          defaultProps={{content, format: "portrait" as const}}
        />
      </Folder>
      <Composition
        id="NarratedCover"
        component={CoverStill}
        durationInFrames={1}
        fps={content.fps}
        width={1280}
        height={720}
        defaultProps={{content}}
      />
    </>
  );
};
