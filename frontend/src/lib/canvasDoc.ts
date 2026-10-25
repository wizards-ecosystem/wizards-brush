import type { CanvasDocument, CanvasLayer, CanvasTransform } from "../api/types";

/**
 * The canvas editor's document state: the document, bounded undo/redo, and
 * the selected layer.
 *
 * Undo and autosave are separate. Undo restores the person's own actions
 * within this session, up to HISTORY_LIMIT steps; autosave persists whatever
 * the document is now. Loading a revision from the server (open, reload after
 * a conflict) starts a fresh history, because undoing past a document someone
 * else saved would silently revert their work.
 */
export const HISTORY_LIMIT = 50;

export interface DocState {
  doc: CanvasDocument;
  past: CanvasDocument[];
  future: CanvasDocument[];
  selected: string | null;
}

export type DocAction =
  | { type: "load"; doc: CanvasDocument }
  | { type: "select"; id: string | null }
  | { type: "add"; layer: Omit<CanvasLayer, "id"> }
  | { type: "remove"; id: string }
  | { type: "move"; id: string; by: number }
  | { type: "update"; id: string; patch: Partial<Omit<CanvasLayer, "id" | "transform">> }
  | { type: "transform"; id: string; patch: Partial<CanvasTransform>; merge?: boolean }
  | { type: "undo" }
  | { type: "redo" };

export const IDENTITY: CanvasTransform = { x: 0, y: 0, scale_x: 1, scale_y: 1, rotation: 0 };

export function newDocument(width: number, height: number): CanvasDocument {
  return { schema_version: 1, width, height, color_space: "srgb", background: "transparent", layers: [] };
}

export function initialState(doc: CanvasDocument): DocState {
  return { doc, past: [], future: [], selected: doc.layers[doc.layers.length - 1]?.id ?? null };
}

/** The next free `layer-N` id. */
export function nextLayerId(doc: CanvasDocument): string {
  const taken = new Set(doc.layers.map((l) => l.id));
  let n = doc.layers.length + 1;
  while (taken.has(`layer-${n}`)) n += 1;
  return `layer-${n}`;
}

function commit(state: DocState, doc: CanvasDocument, merge = false): DocState {
  // A drag sends many small moves; `merge` folds them into the step that began it.
  const past = merge && state.past.length ? state.past : [...state.past, state.doc].slice(-HISTORY_LIMIT);
  return { ...state, doc, past, future: [] };
}

function withLayer(
  doc: CanvasDocument,
  id: string,
  change: (layer: CanvasLayer) => CanvasLayer,
): CanvasDocument {
  return { ...doc, layers: doc.layers.map((layer) => (layer.id === id ? change(layer) : layer)) };
}

export function docReducer(state: DocState, action: DocAction): DocState {
  switch (action.type) {
    case "load":
      return { ...initialState(action.doc), selected: state.selected };
    case "select":
      return { ...state, selected: action.id };
    case "add": {
      const layer = { ...action.layer, id: nextLayerId(state.doc) } as CanvasLayer;
      return { ...commit(state, { ...state.doc, layers: [...state.doc.layers, layer] }), selected: layer.id };
    }
    case "remove": {
      const layers = state.doc.layers.filter((l) => l.id !== action.id);
      return {
        ...commit(state, { ...state.doc, layers }),
        selected: state.selected === action.id ? (layers[layers.length - 1]?.id ?? null) : state.selected,
      };
    }
    case "move": {
      const layers = [...state.doc.layers];
      const index = layers.findIndex((l) => l.id === action.id);
      const target = index + action.by;
      if (index < 0 || target < 0 || target >= layers.length) return state;
      const [layer] = layers.splice(index, 1);
      layers.splice(target, 0, layer);
      return commit(state, { ...state.doc, layers });
    }
    case "update":
      return commit(
        state,
        withLayer(state.doc, action.id, (l) => ({ ...l, ...action.patch })),
      );
    case "transform":
      return commit(
        state,
        withLayer(state.doc, action.id, (l) => ({ ...l, transform: { ...l.transform, ...action.patch } })),
        action.merge,
      );
    case "undo": {
      const previous = state.past[state.past.length - 1];
      if (!previous) return state;
      return { ...state, doc: previous, past: state.past.slice(0, -1), future: [state.doc, ...state.future] };
    }
    case "redo": {
      const [next, ...rest] = state.future;
      if (!next) return state;
      return { ...state, doc: next, past: [...state.past, state.doc].slice(-HISTORY_LIMIT), future: rest };
    }
  }
}

/**
 * A point on the displayed stage in document pixels.
 *
 * Pointer events and getBoundingClientRect are both in CSS pixels, so the
 * screen's devicePixelRatio cancels out: only the stage's display scale (CSS
 * width over document width) maps between them. Using device pixels here
 * would put a drag twice as far on a 2x screen.
 */
export function toDocPoint(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  doc: Pick<CanvasDocument, "width" | "height">,
): { x: number; y: number } {
  return {
    x: ((clientX - rect.left) * doc.width) / rect.width,
    y: ((clientY - rect.top) * doc.height) / rect.height,
  };
}
