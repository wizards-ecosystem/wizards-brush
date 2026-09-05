import { describe, expect, it } from "vitest";
import { estimateCombinations, extractTokens } from "./wildcards";

describe("extractTokens", () => {
  it("finds alternation groups and wildcard names", () => {
    const t = extractTokens("a {red|green|blue} __animal__ in {day|night}");
    expect(t.alts).toEqual([
      ["red", "green", "blue"],
      ["day", "night"],
    ]);
    expect(t.wildcards).toEqual(["animal"]);
  });

  it("ignores braces without alternatives", () => {
    expect(extractTokens("weight {1.2}").alts).toEqual([]);
  });

  it("does not advertise path-like names the backend rejects", () => {
    expect(extractTokens("__folder/name__ __valid-name__").wildcards).toEqual(["valid-name"]);
  });
});

describe("estimateCombinations", () => {
  it("multiplies group sizes", () => {
    expect(estimateCombinations("{a|b} and {x|y|z}")).toBe(6);
  });
  it("is 1 with no groups", () => {
    expect(estimateCombinations("plain prompt __animal__")).toBe(1);
  });
});
