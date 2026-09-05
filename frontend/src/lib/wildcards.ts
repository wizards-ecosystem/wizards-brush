/** Client-side helpers for {a|b|c} variants and __file__ wildcards.
 * Mirrors the backend's prompt_engine syntax for live combination estimates. */

const ALT_RE = /\{([^{}]+)\}/g;
const FILE_RE = /__([a-zA-Z0-9_-]+)__/g;

export interface PromptTokens {
  alts: string[][]; // each {a|b|c} group's options
  wildcards: string[]; // referenced __file__ names
}

export function extractTokens(prompt: string): PromptTokens {
  const alts: string[][] = [];
  const wildcards: string[] = [];
  for (const m of prompt.matchAll(ALT_RE)) {
    const opts = m[1].split("|").map((s) => s.trim());
    if (opts.length > 1) alts.push(opts);
  }
  for (const m of prompt.matchAll(FILE_RE)) wildcards.push(m[1]);
  return { alts, wildcards };
}

/** Server-side cap on combinatorial fan-out (expand_all's cap in prompt_engine.py). */
export const COMBO_CAP = 32;

/** Combinations produced by {a|b|c} groups only — what "combinatorial" fans out.
 * __wildcards__ are seed-keyed picks (not fanned), so they don't multiply here. */
export function estimateCombinations(prompt: string): number {
  const { alts } = extractTokens(prompt);
  return alts.reduce((acc, g) => acc * g.length, 1);
}
