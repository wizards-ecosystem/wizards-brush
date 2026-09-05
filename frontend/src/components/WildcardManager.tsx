import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { WildcardInfo } from "../api/types";
import { useStore } from "../store/useStore";
import { Button, ConfirmDialog, Modal, NameDialog } from "./ui";

/** CRUD for __wildcard__ files (one option per line), used by the prompt engine. */
export function WildcardManager() {
  const toast = useStore((s) => s.toast);
  const [list, setList] = useState<WildcardInfo[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [naming, setNaming] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = () =>
    api
      .wildcards()
      .then(setList)
      .catch(() => setList([]));
  useEffect(() => {
    load();
  }, []);

  const openEditor = async (name: string) => {
    try {
      const w = await api.wildcard(name);
      setText(w.items.join("\n"));
      setEditing(name);
    } catch (e) {
      toast(`Load failed: ${e}`, "error");
    }
  };

  const save = async () => {
    if (!editing) return;
    const items = text
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      await api.saveWildcard(editing, items);
      toast(`__${editing}__ saved (${items.length} entries)`, "success");
      setEditing(null);
      load();
    } catch (e) {
      toast(`Save failed: ${e}`, "error");
    }
  };

  const remove = async (name: string) => {
    try {
      await api.deleteWildcard(name);
      toast(`__${name}__ deleted`, "info");
    } catch (e) {
      toast(`Delete failed: ${e}`, "error");
    }
    load();
  };

  const create = async (name: string) => {
    const clean = name.toLowerCase().replace(/[^a-z0-9_-]/g, "_");
    await api.saveWildcard(clean, []);
    await load();
    openEditor(clean);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="max-w-xl text-xs leading-relaxed text-muted">
          Use <code className="technical text-accent">__name__</code> in a prompt to choose a seed-keyed
          random line. Inline <code className="technical text-accent">{"{a|b|c}"}</code> variants work
          alongside wildcard files.
        </p>
        <Button size="sm" icon="plus" onClick={() => setNaming(true)}>
          New file
        </Button>
      </div>
      {list.length === 0 ? (
        <div className="border-y border-edge py-5 text-xs text-muted">No wildcard files yet.</div>
      ) : (
        <div className="divide-y divide-edge border-y border-edge">
          {list.map((w) => (
            <div key={w.name} className="flex items-center justify-between gap-3 py-2.5 text-sm">
              <button
                className="min-w-0 truncate text-left hover:text-accent"
                onClick={() => openEditor(w.name)}
              >
                <span className="technical">__{w.name}__</span>
                <span className="technical ml-2 text-[10px] text-muted">{w.count} entries</span>
              </button>
              <Button
                variant="quiet"
                size="sm"
                icon="trash"
                className="text-danger"
                onClick={() => setDeleting(w.name)}
              >
                Delete
              </Button>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <Modal onClose={() => setEditing(null)} label={`Edit wildcard ${editing}`}>
          <div className="space-y-4 p-5">
            <div>
              <div className="page-kicker">Wildcard file</div>
              <div className="technical text-lg font-semibold">__{editing}__</div>
            </div>
            <label className="label" htmlFor="wildcard-lines">
              One option per line
            </label>
            <textarea
              id="wildcard-lines"
              className="input technical min-h-[280px] text-xs"
              value={text}
              placeholder="one option per line"
              onChange={(e) => setText(e.target.value)}
            />
            <div className="flex justify-end gap-2">
              <Button variant="quiet" onClick={() => setEditing(null)}>
                Cancel
              </Button>
              <Button variant="primary" icon="check" onClick={save}>
                Save
              </Button>
            </div>
          </div>
        </Modal>
      )}
      {naming && (
        <NameDialog
          title="New wildcard file"
          placeholder="e.g. artist, mood, lens"
          onSubmit={create}
          onClose={() => setNaming(false)}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Delete wildcard __${deleting}__?`}
          message="Prompts referencing it will keep the literal __name__ text."
          onConfirm={() => remove(deleting)}
          onClose={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
