// Per-target marks for the Publish tab's provider cards (shell/AppPublish.tsx).
//
// Same house style and same reasoning as ProviderIcons: viewBox 0 0 24 24, fill
// none, stroke currentColor, round caps/joins, hand-written paths, no npm
// dependency — and deliberately MONOCHROME and shape-suggestive rather than
// brand artwork. It matches the rest of the shell's iconography, survives
// light/dark without a second asset, and keeps us clear of shipping someone
// else's trademarked logo file.
//
// Keyed by publish-target id, which is the one thing about a provider that a
// capability set cannot tell us: which company it is. Everything else on the
// card — whether it is offered, whether it needs funding — comes from the
// adapter's shape, and this does not pretend to.
import type { ReactNode } from "react";

const svgProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  // Heavier than ProviderIcons' 1.15 because these render at ~15px rather than
  // ~40px: the same absolute stroke would come out three times as fine as the
  // lucide glyphs sitting a few pixels away.
  strokeWidth: 1.7,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

const GLYPHS: Record<string, ReactNode> = {
  // Cloudflare: a cloud, which is the shape the name already is.
  "cloudflare-pages": (
    <path d="M6.5 17 H17.5 a3.2 3.2 0 0 0 .2 -6.4 A5.6 5.6 0 0 0 6.9 9.5 a3.8 3.8 0 0 0 -.4 7.5 Z" />
  ),
  // The Internet Computer: the infinity loop it is marked with — two turns of
  // one continuous line, which is also roughly what the network is.
  "icp-canister": (
    <path d="M8 7.5 c2.9 0 4.9 9 8 9 a4.5 4.5 0 0 0 0 -9 c-2.9 0 -4.9 9 -8 9 a4.5 4.5 0 0 1 0 -9 Z" />
  ),
};

// A globe for a provider we have no mark for. Unlike ProviderIcons this record
// CANNOT be total — target ids come down from the server, so a provider added
// on the Python side alone would otherwise leave a hole in the row where every
// other card has a badge.
const FALLBACK: ReactNode = (
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M3.5 12 h17" />
    <path d="M12 3.5 C14.4 6.2 15.6 9 15.6 12 C15.6 15 14.4 17.8 12 20.5 C9.6 17.8 8.4 15 8.4 12 C8.4 9 9.6 6.2 12 3.5 Z" />
  </>
);

export function PublishTargetIcon({ target }: { target: string }) {
  return <svg {...svgProps}>{GLYPHS[target] ?? FALLBACK}</svg>;
}
