import {existsSync} from "node:fs";
import {Config} from "@remotion/cli/config";

const candidates = [
  process.env.VIDEO_STUDIO_CHROMIUM,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/opt/google/chrome/google-chrome",
].filter((candidate): candidate is string => Boolean(candidate));

const browser = candidates.find((candidate) => existsSync(candidate));
if (!browser) {
  throw new Error(
    "Local Chrome/Chromium not found. Set VIDEO_STUDIO_CHROMIUM; " +
      "network browser downloads are disabled for this template.",
  );
}

Config.setBrowserExecutable(browser);
