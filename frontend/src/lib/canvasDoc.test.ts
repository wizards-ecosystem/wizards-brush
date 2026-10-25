import { describe, expect, it } from "vitest";
import type { CanvasLayer } from "../api/types";
import { HISTORY_LIMIT, IDENTITY, docReducer, initialState, newDocument, toDocPoint } from "./canvasDoc";

const layer = (asset_id: number): Omit<CanvasLayer, "id"> => ({
  name: `a${asset_id}`,
  asset_id,
  mask_id: null,
  transform: { ...IDENTITY },
  opacity: 1,
  visible: true,
  blend_mode: "normal",
});

describe("the canvas document reducer", () => {
  it("adds layers on top with fresh ids, reorders and removes them", () => {
    let state = initialState(newDocument(100, 50));
    state = docReducer(state, { type: "add", layer: layer(1) });
    state = docReducer(state, { type: "add", layer: layer(2) });
    expect(state.doc.layers.map((l) => l.id)).toEqual(["layer-1", "layer-2"]);
    expect(state.selected).toBe("layer-2");
    state = docReducer(state, { type: "move", id: "layer-2", by: -1 });
    expect(state.doc.layers.map((l) => l.id)).toEqual(["layer-2", "layer-1"]);
    expect(docReducer(state, { type: "move", id: "layer-2", by: -1 })).toBe(state); // already bottom
    state = docReducer(state, { type: "remove", id: "layer-1" });
    expect(state.doc.layers.map((l) => l.id)).toEqual(["layer-2"]);
    state = docReducer(state, { type: "add", layer: layer(3) });
    expect(state.doc.layers.map((l) => l.id)).toEqual(["layer-2", "layer-3"]);
  });

  it("undoes and redoes, and a new action clears redo", () => {
    let state = initialState(newDocument(10, 10));
    state = docReducer(state, { type: "add", layer: layer(1) });
    state = docReducer(state, { type: "transform", id: "layer-1", patch: { x: 5, rotation: 30 } });
    expect(state.doc.layers[0].transform).toMatchObject({ x: 5, y: 0, rotation: 30, scale_x: 1 });
    state = docReducer(state, { type: "undo" });
    expect(state.doc.layers[0].transform.x).toBe(0);
    state = docReducer(state, { type: "redo" });
    expect(state.doc.layers[0].transform.x).toBe(5);
    state = docReducer(state, { type: "undo" });
    state = docReducer(state, { type: "update", id: "layer-1", patch: { opacity: 0.5 } });
    expect(state.future).toEqual([]);
  });

  it(`keeps at most ${HISTORY_LIMIT} undo steps`, () => {
    let state = initialState(newDocument(10, 10));
    state = docReducer(state, { type: "add", layer: layer(1) });
    for (let i = 1; i <= HISTORY_LIMIT + 20; i += 1) {
      state = docReducer(state, { type: "transform", id: "layer-1", patch: { x: i } });
    }
    expect(state.past).toHaveLength(HISTORY_LIMIT);
    for (let i = 0; i < HISTORY_LIMIT + 5; i += 1) state = docReducer(state, { type: "undo" });
    expect(state.doc.layers[0].transform.x).toBe(20);
  });

  it("folds one drag into a single undo step", () => {
    let state = initialState(newDocument(10, 10));
    state = docReducer(state, { type: "add", layer: layer(1) });
    state = docReducer(state, { type: "transform", id: "layer-1", patch: { x: 1 }, merge: false });
    state = docReducer(state, { type: "transform", id: "layer-1", patch: { x: 2 }, merge: true });
    state = docReducer(state, { type: "transform", id: "layer-1", patch: { x: 3 }, merge: true });
    state = docReducer(state, { type: "undo" });
    expect(state.doc.layers[0].transform.x).toBe(0);
  });

  it("loading a saved revision starts a fresh history", () => {
    let state = initialState(newDocument(10, 10));
    state = docReducer(state, { type: "add", layer: layer(1) });
    state = docReducer(state, { type: "load", doc: newDocument(20, 20) });
    expect(state.past).toEqual([]);
    expect(docReducer(state, { type: "undo" })).toBe(state);
  });
});

describe("stage coordinates", () => {
  it("map CSS pixels to document pixels by the display scale alone", () => {
    // A 2000x1000 document shown 500 CSS px wide: one CSS px is 4 document px,
    // whatever the screen's devicePixelRatio.
    const rect = { left: 100, top: 50, width: 500, height: 250 };
    expect(toDocPoint(100, 50, rect, { width: 2000, height: 1000 })).toEqual({ x: 0, y: 0 });
    expect(toDocPoint(350, 175, rect, { width: 2000, height: 1000 })).toEqual({ x: 1000, y: 500 });
  });
});
