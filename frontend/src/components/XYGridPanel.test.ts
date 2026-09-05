import { describe, expect, it } from "vitest";
import type { Control } from "../api/types";
import { parseValues } from "./XYGridPanel";

const guidance: Control = {
  name: "guidance",
  label: "Guidance",
  type: "slider",
  default: 1,
  min: 0,
  max: 8,
  step: 0.1,
};

describe("parameter-study value syntax", () => {
  it("previews step and exact-count ranges", () => {
    expect(parseValues("1-2 (+0.5)", guidance)).toEqual([1, 1.5, 2]);
    expect(parseValues("1-2 [5]", guidance)).toEqual([1, 1.25, 1.5, 1.75, 2]);
  });

  it("mixes compact ranges with plain values", () => {
    expect(parseValues("0, 1-2 (+0.5), 4", guidance)).toEqual([0, 1, 1.5, 2, 4]);
  });

  it("treats quoted commas as one prompt replacement", () => {
    expect(parseValues('"red, gold", blue')).toEqual(["red, gold", "blue"]);
  });
});
