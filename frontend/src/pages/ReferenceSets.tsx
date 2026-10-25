import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { Asset, ReferenceSetDefinition, ReferenceSetRead, ReferenceSetSummary } from "../api/types";
import { GalleryPicker } from "../components/GalleryPicker";
import { Icon } from "../components/icons";
import { Button, EmptyState, PageHeader, Spinner } from "../components/ui";
import { useStore } from "../store/useStore";

const ROLE_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;

function blankDefinition(): ReferenceSetDefinition {
  return {
    description: "",
    notes: "",
    roles: [{ role: "character", label: "", entries: [] }],
    representatives: [],
  };
}

function Thumb({ id, className = "size-16" }: { id: number; className?: string }) {
  const recent = useStore((s) => s.assets);
  const cached = recent.find((a) => a.id === id) ?? null;
  const [asset, setAsset] = useState<Asset | null>(cached);
  useEffect(() => {
    if (cached) return;
    let alive = true;
    api
      .asset(id)
      .then((a) => alive && setAsset(a))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [id, cached]);
  const shown = cached ?? asset;
  return (
    <div className={`${className} shrink-0 border border-edge bg-black`}>
      {shown?.thumb_url || shown?.url ? (
        <img src={shown.thumb_url ?? shown.url} alt={`asset ${id}`} className="size-full object-contain" />
      ) : (
        <span className="technical flex size-full items-center justify-center text-[10px] text-muted">
          #{id}
        </span>
      )}
    </div>
  );
}

/** Edit one set. Saving writes the next revision only if nobody saved since. */
function Editor({
  existing,
  onSaved,
}: {
  existing: ReferenceSetRead | null;
  onSaved: (read: ReferenceSetRead) => void;
}) {
  const toast = useStore((s) => s.toast);
  const [name, setName] = useState(existing?.name ?? "");
  const [definition, setDefinition] = useState<ReferenceSetDefinition>(
    existing ? structuredClone(existing.definition) : blankDefinition(),
  );
  const [picking, setPicking] = useState<{ role: number; mask?: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<number | null>(null);

  const setRoles = (roles: ReferenceSetDefinition["roles"]) => {
    const members = new Set(roles.flatMap((r) => r.entries.map((e) => e.asset_id)));
    setDefinition((d) => ({
      ...d,
      roles,
      representatives: (d.representatives ?? []).filter((a) => members.has(a)),
    }));
  };
  const roleAt = (index: number, patch: Partial<ReferenceSetDefinition["roles"][number]>) =>
    setRoles(definition.roles.map((r, n) => (n === index ? { ...r, ...patch } : r)));
  const moveEntry = (roleIndex: number, index: number, by: number) => {
    const entries = [...definition.roles[roleIndex].entries];
    const [item] = entries.splice(index, 1);
    entries.splice(index + by, 0, item);
    roleAt(roleIndex, { entries });
  };
  const toggleRepresentative = (assetId: number) =>
    setDefinition((d) => {
      const current = d.representatives ?? [];
      return {
        ...d,
        representatives: current.includes(assetId)
          ? current.filter((a) => a !== assetId)
          : [...current, assetId].slice(-4),
      };
    });

  const badRole = definition.roles.find((r) => !ROLE_PATTERN.test(r.role));
  const duplicate = new Set(definition.roles.map((r) => r.role)).size !== definition.roles.length;
  const empty = definition.roles.some((r) => r.entries.length === 0);
  const problem = !name.trim()
    ? "Name the set."
    : badRole
      ? `Role "${badRole.role}" must be lowercase letters, digits or _.`
      : duplicate
        ? "Each role appears once."
        : empty
          ? "Every role needs at least one image."
          : "";

  const save = async () => {
    setSaving(true);
    setConflict(null);
    try {
      const read = existing
        ? await api.updateReferenceSet(existing.id, definition, existing.head_revision, name.trim())
        : await api.createReferenceSet(name.trim(), definition);
      toast(existing ? `Saved revision ${read.revision}` : "Reference set created", "success");
      onSaved(read);
    } catch (error) {
      if (error instanceof ApiError && error.code === "revision_conflict") {
        setConflict(Number(error.state?.current_revision ?? 0));
      } else {
        toast(`Could not save: ${error}`, "error");
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-5" data-testid="reference-editor">
      <div className="grid gap-3 md:grid-cols-2">
        <label>
          <span className="label">Name</span>
          <input className="input" value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          <span className="label">Description</span>
          <input
            className="input"
            value={definition.description ?? ""}
            onChange={(event) => setDefinition((d) => ({ ...d, description: event.target.value }))}
          />
        </label>
      </div>
      <label className="block">
        <span className="label">Notes</span>
        <textarea
          className="input min-h-16"
          placeholder="Distinguishing details. A preset role that asks for notes places this text in its prompt."
          value={definition.notes ?? ""}
          onChange={(event) => setDefinition((d) => ({ ...d, notes: event.target.value }))}
        />
      </label>

      {definition.roles.map((role, roleIndex) => (
        <section key={roleIndex} className="space-y-2 border border-edge p-3" data-testid="reference-role">
          <div className="flex flex-wrap items-end gap-2">
            <label className="w-40">
              <span className="label">Role</span>
              <input
                className="input technical text-xs"
                value={role.role}
                onChange={(event) => roleAt(roleIndex, { role: event.target.value.trim().toLowerCase() })}
              />
            </label>
            <label className="min-w-0 flex-1">
              <span className="label">Label</span>
              <input
                className="input text-xs"
                value={role.label ?? ""}
                onChange={(event) => roleAt(roleIndex, { label: event.target.value })}
              />
            </label>
            {definition.roles.length > 1 && (
              <Button size="sm" onClick={() => setRoles(definition.roles.filter((_, n) => n !== roleIndex))}>
                Remove role
              </Button>
            )}
          </div>
          <ol className="space-y-2">
            {role.entries.map((entry, index) => (
              <li
                key={`${entry.asset_id}-${index}`}
                className="flex items-start gap-2"
                data-testid="reference-entry"
              >
                <span className="technical w-5 pt-1 text-[11px] text-muted">{index + 1}</span>
                <Thumb id={entry.asset_id} />
                {entry.mask_id ? <Thumb id={entry.mask_id} className="size-10" /> : null}
                <div className="min-w-0 flex-1 space-y-1">
                  <input
                    className="input py-1 text-xs"
                    placeholder="Note (optional)"
                    aria-label={`Note for reference ${index + 1}`}
                    value={entry.note ?? ""}
                    onChange={(event) =>
                      roleAt(roleIndex, {
                        entries: role.entries.map((e, n) =>
                          n === index ? { ...e, note: event.target.value } : e,
                        ),
                      })
                    }
                  />
                  <div className="flex flex-wrap gap-1">
                    <button
                      type="button"
                      className="chip text-[10px]"
                      onClick={() =>
                        entry.mask_id
                          ? roleAt(roleIndex, {
                              entries: role.entries.map((e, n) =>
                                n === index ? { ...e, mask_id: null } : e,
                              ),
                            })
                          : setPicking({ role: roleIndex, mask: index })
                      }
                    >
                      {entry.mask_id ? "Remove mask" : "Add mask"}
                    </button>
                    <button
                      type="button"
                      className={`chip text-[10px] ${(definition.representatives ?? []).includes(entry.asset_id) ? "border-accent text-ink" : ""}`}
                      aria-pressed={(definition.representatives ?? []).includes(entry.asset_id)}
                      onClick={() => toggleRepresentative(entry.asset_id)}
                    >
                      Representative
                    </button>
                  </div>
                </div>
                <span className="flex shrink-0 gap-0.5">
                  <button
                    type="button"
                    className="icon-btn size-6 min-h-0"
                    aria-label={`Move reference ${index + 1} up`}
                    disabled={index === 0}
                    onClick={() => moveEntry(roleIndex, index, -1)}
                  >
                    <Icon name="arrow-up" size={11} />
                  </button>
                  <button
                    type="button"
                    className="icon-btn size-6 min-h-0"
                    aria-label={`Move reference ${index + 1} down`}
                    disabled={index === role.entries.length - 1}
                    onClick={() => moveEntry(roleIndex, index, 1)}
                  >
                    <Icon name="arrow-down" size={11} />
                  </button>
                  <button
                    type="button"
                    className="icon-btn size-6 min-h-0"
                    aria-label={`Remove reference ${index + 1}`}
                    onClick={() => roleAt(roleIndex, { entries: role.entries.filter((_, n) => n !== index) })}
                  >
                    <Icon name="close" size={11} />
                  </button>
                </span>
              </li>
            ))}
          </ol>
          {role.entries.length < 16 && (
            <Button size="sm" icon="plus" onClick={() => setPicking({ role: roleIndex })}>
              Add from gallery
            </Button>
          )}
        </section>
      ))}
      {definition.roles.length < 8 && (
        <Button
          size="sm"
          icon="plus"
          onClick={() =>
            setRoles([
              ...definition.roles,
              { role: `role_${definition.roles.length + 1}`, label: "", entries: [] },
            ])
          }
        >
          Add a role
        </Button>
      )}

      {conflict !== null && (
        <div className="border-l-2 border-warn pl-2 text-xs text-warn" data-testid="reference-conflict">
          Someone saved revision {conflict} after you opened this set. Nothing was overwritten; reload it to
          see their change, then reapply yours.
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" loading={saving} disabled={Boolean(problem)} onClick={() => void save()}>
          {existing ? `Save as revision ${existing.head_revision + 1}` : "Create set"}
        </Button>
        {problem && <span className="text-[11px] text-muted">{problem}</span>}
      </div>

      {picking && (
        <GalleryPicker
          title={picking.mask !== undefined ? "Choose a mask" : "Choose a reference image"}
          filter={(a) =>
            a.kind === "image" &&
            (picking.mask !== undefined ? a.generator === "mask" : a.generator !== "mask")
          }
          onClose={() => setPicking(null)}
          onPick={(asset) => {
            const role = definition.roles[picking.role];
            if (picking.mask !== undefined) {
              const target = role.entries[picking.mask];
              const source = useStore.getState().assets.find((a) => a.id === target.asset_id);
              if (source && (source.width !== asset.width || source.height !== asset.height)) {
                toast("A mask must be the same size as its image", "error");
                return;
              }
              roleAt(picking.role, {
                entries: role.entries.map((e, n) => (n === picking.mask ? { ...e, mask_id: asset.id } : e)),
              });
            } else {
              roleAt(picking.role, { entries: [...role.entries, { asset_id: asset.id }] });
            }
            setPicking(null);
          }}
        />
      )}
    </div>
  );
}

/**
 * Reference sets: named, ordered references by role (a character, a product,
 * a style). Reference material only: no model is promised to keep an
 * identity, and each preset says how many references a role takes.
 */
export function ReferenceSets() {
  const { id } = useParams();
  const navigate = useNavigate();
  const toast = useStore((s) => s.toast);
  const [sets, setSets] = useState<ReferenceSetSummary[] | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [open, setOpen] = useState<ReferenceSetRead | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(
    () =>
      api
        .referenceSets(showArchived)
        .then(setSets)
        .catch((e) => setError(`${e}`)),
    [showArchived],
  );
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (!id || id === "new") return;
    let alive = true;
    api
      .referenceSet(Number(id))
      .then((read) => alive && setOpen(read))
      .catch((e) => alive && setError(`${e}`));
    return () => {
      alive = false;
    };
  }, [id]);

  const archive = async (read: ReferenceSetRead) => {
    try {
      const next = await api.archiveReferenceSet(read.id, !read.archived);
      setOpen(next);
      void load();
      toast(next.archived ? "Archived; old plans can still read it" : "Restored", "info");
    } catch (e) {
      toast(`${e}`, "error");
    }
  };

  // The fetched set counts only while the route still names it.
  const current = open !== null && String(open.id) === id ? open : null;
  const editing = id === "new" || current !== null;
  return (
    <div className="page-shell page-pad mx-auto max-w-5xl">
      <PageHeader
        kicker="Library"
        title="Reference sets"
        description="Ordered references by role for presets that take them. A set is reference material: it is not training, and no model is promised to keep an identity."
        actions={
          <Button icon="plus" onClick={() => navigate("/references/new")}>
            New set
          </Button>
        }
      />
      {error && <div className="mt-4 border-l-2 border-danger pl-2 text-xs text-danger">{error}</div>}
      <div className="mt-6 grid items-start gap-8 lg:grid-cols-[260px_minmax(0,1fr)]">
        <div className="space-y-2">
          <label className="flex items-center gap-2 text-[11px] text-muted">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(event) => setShowArchived(event.target.checked)}
            />
            Show archived
          </label>
          {sets === null ? (
            <div className="flex items-center gap-2 text-sm text-muted">
              <Spinner /> Loading…
            </div>
          ) : sets.length === 0 ? (
            <div className="text-xs text-muted">No reference sets yet.</div>
          ) : (
            <div className="divide-y divide-edge border border-edge" data-testid="reference-sets">
              {sets.map((set) => (
                <Link
                  key={set.id}
                  to={`/references/${set.id}`}
                  className={`flex items-center gap-2 px-3 py-2 text-sm ${String(set.id) === id ? "bg-accent/12" : "hover:bg-panel2"}`}
                >
                  {set.representatives[0] ? <Thumb id={set.representatives[0]} className="size-8" /> : null}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{set.name}</span>
                    <span className="technical text-[10px] text-muted">
                      {set.roles.map((r) => `${r.role} ×${r.count}`).join(" · ")} · r{set.head_revision}
                      {set.archived ? " · archived" : ""}
                    </span>
                  </span>
                </Link>
              ))}
            </div>
          )}
        </div>
        <div className="min-w-0">
          {!editing ? (
            <EmptyState
              icon="image"
              title="Choose or create a set"
              description="Add gallery images under a role, in the order that matters, with optional masks and notes."
            />
          ) : (
            <>
              {current && (
                <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted">
                  <span className="technical">
                    revision {current.revision} of {current.head_revision} · {current.digest.slice(0, 19)}
                  </span>
                  <Button size="sm" onClick={() => void archive(current)}>
                    {current.archived ? "Unarchive" : "Archive"}
                  </Button>
                </div>
              )}
              <Editor
                key={current ? `${current.id}:${current.revision}` : "new"}
                existing={current}
                onSaved={(read) => {
                  setOpen(read);
                  void load();
                  navigate(`/references/${read.id}`);
                }}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
