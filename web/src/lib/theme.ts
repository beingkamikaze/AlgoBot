import { ColorType } from "lightweight-charts";

/** Chart colours: the same values as the CSS tokens in globals.css (canvas charts cannot read CSS classes). */
export const C = {
  canvas: "#0b0d10",
  surface: "#111418",
  line: "#232931",
  grid: "#171b21",
  text: "#9ba5b0",
  subtle: "#7d8792",
  fg: "#e6e9ed",
  accent: "#2bb8a6",
  up: "#2ebd85",
  down: "#f0596d",
  warn: "#e5a93b",
  info: "#6aa6f8",
  // indicator overlays: distinct from up/down so they never read as gains or losses
  series: ["#6aa6f8", "#e5a93b", "#b48cf2", "#4fc3d9", "#e07bb5", "#a3c86b"],
} as const;

export const CHART_FONT = "IBM Plex Mono, ui-monospace, monospace";

/** Common lightweight-charts options for the dark terminal look. */
export function chartBase() {
  return {
    layout: {
      background: { type: ColorType.Solid, color: "transparent" },
      textColor: C.text,
      fontFamily: CHART_FONT,
      fontSize: 11,
      attributionLogo: false,
      panes: { separatorColor: C.line, separatorHoverColor: C.line },
    },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    rightPriceScale: { borderColor: C.line },
    timeScale: { borderColor: C.line },
    crosshair: {
      vertLine: { color: C.subtle, labelBackgroundColor: "#232931" },
      horzLine: { color: C.subtle, labelBackgroundColor: "#232931" },
    },
  };
}
