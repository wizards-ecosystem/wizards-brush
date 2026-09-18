import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { VariantRecipe, VariantSetSummary } from "../api/types";
import { Icon } from "../components/icons";
import { Button, ConfirmDialog, EmptyState, PageHeader } from "../components/ui";
import { countCombinations, settledFraction } from "../lib/variantSets";
import { useStore } from "../store/useStore";

const STATUS_TONE: Record<string, string> = {
  active: "text-accent2",
  complete: "text-ok",
  incomplete: "text-warn",
  canceled: "text-muted",
};

/** Every Variant Set run so far, and the saved recipes new ones start from. */
export function VariantSets() {
  const navigate = useNavigate();
  const revision = useStore((s) => s.variantRevision);
  const toast = useStore((s) => s.toast);
  const [sets, setSets] = useState<VariantSetSummary[] | null>(null);
  const [recipes, setRecipes] = useState<VariantRecipe[]>([]);
  const [deleting, setDeleting] = useState<VariantRecipe | null>(null);

  const load = useCallback(() => {
    api
      .variantSets()
      .then(setSets)
      .catch((e) => toast(`Couldn't load variant sets: ${e}`, "error"));
    api
      .variantRecipes()
      .then(setRecipes)
      .catch(() => setRecipes([]));
  }, [toast]);

  useEffect(() => {
    const timer = setTimeout(load, sets ? 500 : 0);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, revision]);

  const clone = async (recipe: VariantRecipe) => {
    try {
      await api.cloneVariantRecipe(recipe.id);
      toast("Recipe cloned", "success");
      load();
    } catch (e) {
      toast(`Couldn't clone: ${e}`, "error");
    }
  };

  return (
    <div className="page-pad h-full overflow-y-auto">
      <PageHeader
        kicker="Workshop"
        title="Variant sets"
        description="One source, named axes, one instruction: every combination queued, tracked, validated and exported as a set."
        actions={
          <Button variant="primary" icon="plus" onClick={() => navigate("/variants/new")}>
            New variant set
          </Button>
        }
      />

      <section className="mt-6" aria-label="Variant sets">
        {sets === null ? null : sets.length === 0 ? (
          <div className="flex min-h-56 items-center">
            <EmptyState
              icon="layers"
              title="No variant sets yet"
              description="Start one from a gallery image: choose an operation, add axes such as material × finish × lighting, and every combination becomes its own tracked job."
              action={
                <Button icon="plus" onClick={() => navigate("/variants/new")}>
                  New variant set
                </Button>
              }
            />
          </div>
        ) : (
          <div className="divide-y divide-edge border-y border-edge">
            {sets.map((set) => {
              const c = set.counts;
              return (
                <Link
                  key={set.id}
                  to={`/variants/${set.id}`}
                  className="grid gap-2 px-2 py-3 hover:bg-panel2 sm:grid-cols-[minmax(0,1fr)_auto]"
                >
                  <div className="min-w-0">
                    <div className="flex items-baseline gap-2">
                      <span className="truncate font-medium">{set.name}</span>
                      <span
                        className={`technical text-[10px] uppercase ${STATUS_TONE[set.status] || "text-muted"}`}
                      >
                        {set.status}
                      </span>
                    </div>
                    <div className="technical mt-0.5 text-[10px] text-muted">
                      {set.operation} ·{" "}
                      {set.stages.map((s) => s.axes.map((a) => a.name).join(" × ") || "—").join(" → ")} ·{" "}
                      {new Date(set.created_at).toLocaleString()}
                    </div>
                    <div className="mt-2 h-[3px] max-w-md overflow-hidden bg-edge">
                      <div className="h-full bg-ok" style={{ width: `${settledFraction(c) * 100}%` }} />
                    </div>
                  </div>
                  <div className="technical self-center text-xs text-muted">
                    {c.succeeded || 0}/{set.expected} done
                    {(c.failed || 0) + (c.invalid || 0) > 0 && (
                      <span className="text-danger"> · {(c.failed || 0) + (c.invalid || 0)} failed</span>
                    )}
                  </div>
                </Link>
              );
            })}
          </div>
        )}
      </section>

      <section className="mt-10" aria-label="Saved recipes">
        <h2 className="text-lg font-semibold">Saved recipes</h2>
        <p className="mt-1 text-sm text-muted">
          A recipe is a reusable definition. Editing one never changes a set already made from it.
        </p>
        {recipes.length === 0 ? (
          <div className="mt-3 text-sm text-muted">
            Save a recipe from the editor to reuse its axes, templates and settings.
          </div>
        ) : (
          <div className="mt-3 divide-y divide-edge border-y border-edge">
            {recipes.map((recipe) => (
              <div key={recipe.id} className="flex flex-wrap items-center justify-between gap-2 px-2 py-3">
                <div className="min-w-0">
                  <div className="font-medium">{recipe.name}</div>
                  <div className="technical text-[10px] text-muted">
                    {recipe.recipe.stages.map((s) => s.operation).join(" → ")} ·{" "}
                    {countCombinations(recipe.recipe.stages).total} variants
                  </div>
                </div>
                <div className="flex gap-1">
                  <Button size="sm" icon="make" onClick={() => navigate(`/variants/new?recipe=${recipe.id}`)}>
                    Use
                  </Button>
                  <Button size="sm" variant="quiet" icon="copy" onClick={() => clone(recipe)}>
                    Clone
                  </Button>
                  <Button size="sm" variant="quiet" icon="trash" onClick={() => setDeleting(recipe)}>
                    Delete
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {deleting && (
        <ConfirmDialog
          title={`Delete the recipe "${deleting.name}"?`}
          message="Sets already made from it keep their own copy and are not affected."
          onConfirm={async () => {
            try {
              await api.deleteVariantRecipe(deleting.id);
              load();
            } catch (e) {
              toast(`Couldn't delete: ${e}`, "error");
            }
          }}
          onClose={() => setDeleting(null)}
        />
      )}
      <div className="mt-8 flex items-center gap-2 text-[11px] text-muted">
        <Icon name="info" size={13} /> Sets run as ordinary jobs, so they also appear in the Queue.
      </div>
    </div>
  );
}
