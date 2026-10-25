import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { RecipeRun, SavedRecipe, SavedRecipeSummary } from "../api/types";
import { RecipeForm } from "../components/RecipeForm";
import { Button, ConfirmDialog, EmptyState, PageHeader } from "../components/ui";
import { useStore } from "../store/useStore";

/** Saved generation setups. Running one queues an ordinary job. */
export function Recipes() {
  const { key } = useParams();
  const navigate = useNavigate();
  const toast = useStore((s) => s.toast);
  const refreshJobs = useStore((s) => s.refreshJobs);
  const [rows, setRows] = useState<SavedRecipeSummary[] | null>(null);
  const [loaded, setLoaded] = useState<SavedRecipe | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RecipeRun | null>(null);
  const [resultKey, setResultKey] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const recipe = key != null && loadedKey === key ? loaded : null;
  const missing = key != null && failedKey === key;
  const shown = resultKey === key ? result : null;

  const loadList = useCallback(() => {
    api
      .recipes()
      .then(setRows)
      .catch((error: unknown) => {
        setRows([]);
        toast(`Couldn't load recipes: ${error instanceof Error ? error.message : error}`, "error");
      });
  }, [toast]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  useEffect(() => {
    if (!key) return;
    let cancelled = false;
    api
      .recipe(key)
      .then((row) => {
        if (!cancelled) {
          setLoaded(row);
          setLoadedKey(key);
          setFailedKey(null);
        }
      })
      .catch(() => {
        if (!cancelled) setFailedKey(key);
      });
    return () => {
      cancelled = true;
    };
  }, [key]);

  const run = async (inputs: Record<string, unknown>, dryRun: boolean) => {
    if (!recipe || busy) return;
    setBusy(true);
    try {
      const response = await api.runRecipe(recipe.slug, { inputs, dry_run: dryRun });
      setResult(response);
      setResultKey(key ?? null);
      if (response.dry_run) {
        toast("Check passed. Nothing was queued.", "success");
      } else {
        toast(`Queued job #${response.job_id}`, "success");
        void refreshJobs();
      }
    } catch (error) {
      toast(`${error instanceof Error ? error.message : error}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!recipe) return;
    try {
      await api.deleteRecipe(recipe.slug);
      toast("Recipe deleted", "success");
      setDeleting(false);
      setLoaded(null);
      setLoadedKey(null);
      navigate("/recipes");
      loadList();
    } catch (error) {
      toast(`Couldn't delete: ${error instanceof Error ? error.message : error}`, "error");
    }
  };

  return (
    <div className="page-pad h-full overflow-y-auto">
      <PageHeader
        kicker="Workshop"
        title="Recipes"
        description="A saved setup with only the fields you want to change. Running it queues a normal job."
      />
      <div className="mt-2 flex gap-3 text-xs">
        <span className="text-ink">Saved generators</span>
        <Link to="/presets" className="text-muted underline-offset-2 hover:underline">
          Studio presets
        </Link>
      </div>
      <div className="mt-6 grid items-start gap-6 lg:grid-cols-[minmax(220px,280px)_minmax(0,1fr)]">
        <nav aria-label="Saved recipes" className="min-w-0">
          {rows === null ? null : rows.length === 0 ? (
            <EmptyState
              icon="bookmark"
              title="No recipes yet"
              description="Open a generator, set it up, and choose Save as recipe. Check the fields a later run should ask for."
            />
          ) : (
            <ul className="divide-y divide-edge border border-edge">
              {rows.map((row) => {
                const active = recipe?.id === row.id || key === row.slug || key === String(row.id);
                return (
                  <li key={row.id}>
                    <Link
                      to={`/recipes/${row.slug}`}
                      aria-current={active ? "page" : undefined}
                      className={`block px-3 py-3 hover:bg-panel2 ${active ? "bg-panel2" : ""}`}
                    >
                      <span className="block text-sm font-medium">{row.name}</span>
                      <span className="technical mt-0.5 block text-[10px] uppercase text-muted">
                        {row.kind}
                        {row.fields.length ? ` · ${row.fields.map((field) => field.name).join(", ")}` : ""}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </nav>

        <section aria-label="Run recipe" className="min-w-0 border border-edge bg-panel p-5">
          {!key && (
            <p className="text-sm text-muted">Select a recipe to run it, or save one from a generator.</p>
          )}
          {missing && (
            <p role="alert" className="text-sm text-danger">
              That recipe is not on this machine.
            </p>
          )}
          {recipe && (
            <>
              <RecipeForm key={`${recipe.id}:${recipe.updated_at}`} recipe={recipe} busy={busy} onRun={run} />
              {shown && (
                <p role="status" className="mt-4 text-sm text-muted">
                  {shown.dry_run ? (
                    `Check passed. A real run would queue ${shown.would_queue ?? 0}.`
                  ) : (
                    <>
                      Queued{" "}
                      <Link className="text-accent underline underline-offset-2" to="/queue">
                        job #{shown.job_id}
                      </Link>
                      .
                    </>
                  )}
                </p>
              )}
              <div className="mt-6 border-t border-edge pt-4">
                <Button variant="danger" type="button" onClick={() => setDeleting(true)}>
                  Delete recipe
                </Button>
              </div>
            </>
          )}
        </section>
      </div>
      {deleting && recipe && (
        <ConfirmDialog
          title={`Delete ${recipe.name}?`}
          message="Jobs already queued from it stay in the queue."
          confirmLabel="Delete"
          onClose={() => setDeleting(false)}
          onConfirm={() => void remove()}
        />
      )}
    </div>
  );
}
