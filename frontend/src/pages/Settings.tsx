import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, syncMediaToken } from "../api/client";
import type { AppSettings, UserPreset } from "../api/types";
import { Icon, type IconName } from "../components/icons";
import { WildcardManager } from "../components/WildcardManager";
import { Button, EmptyState, PageHeader, Spinner, fmtBytes } from "../components/ui";
import { useStore } from "../store/useStore";

export function Settings() {
  const refreshSystem = useStore((s) => s.refreshSystem);
  const system = useStore((s) => s.system);
  const refreshPresets = useStore((s) => s.refreshPresets);
  const stats = useStore((s) => s.stats);
  const refreshStats = useStore((s) => s.refreshStats);
  const toast = useStore((s) => s.toast);
  const [data, setData] = useState<AppSettings | null>(null);
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [apiToken, setApiToken] = useState(localStorage.getItem("api_token") || "");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [presets, setPresets] = useState<UserPreset[]>([]);

  const load = () =>
    api.getSettings().then((d) => {
      setData(d);
      setUrl(d.remote_gpu_base_url || "");
    });
  const loadPresets = () => api.userPresets().then(setPresets);
  useEffect(() => {
    load();
    loadPresets();
    refreshStats();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    setSaving(true);
    setMsg("");
    try {
      const res = await api.saveSettings({
        remote_gpu_base_url: url,
        remote_gpu_shared_secret: secret || undefined,
      });
      await refreshSystem();
      await load();
      setSecret("");
      const ok = res.remote_gpu?.connected;
      setMsg(ok ? "Connected to Remote GPU" : `Saved — Remote GPU: ${res.remote_gpu?.reason || "offline"}`);
      toast(ok ? "Connected to Remote GPU" : "Saved (Remote GPU offline)", ok ? "success" : "info");
    } catch (e) {
      setMsg(`Error: ${e}`);
      toast(`${e}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const delPreset = async (id: number) => {
    await api.deletePreset(id);
    await loadPresets();
    await refreshPresets();
    toast("Preset deleted", "info");
  };

  const saveExportSetting = async (name: "embed_metadata" | "embed_provenance", value: boolean) => {
    setData((current) => (current ? { ...current, [name]: value } : current));
    try {
      const updated = await api.saveSettings({ [name]: value });
      setData(updated);
      toast("File export preference saved", "success");
    } catch (error) {
      await load();
      toast(`${error}`, "error");
    }
  };

  if (!data)
    return (
      <div className="page-pad flex items-center gap-3 text-sm text-muted">
        <Spinner /> Loading workshop settings…
      </div>
    );

  return (
    <div className="page-shell page-pad mx-auto max-w-6xl">
      <PageHeader
        kicker="Workshop configuration"
        title="Settings"
        description="Connections, reusable language, model inventory, and local browser access."
      />

      {data.configuration_warning && (
        <div className="mb-5 border-l-2 border-warn bg-warn/8 px-4 py-3 text-xs leading-relaxed text-warn">
          <div className="mb-1 font-medium text-ink">Configuration recovered</div>
          {data.configuration_warning}
        </div>
      )}

      {stats && (
        <dl className="mt-6 grid grid-cols-2 border-y border-edge md:grid-cols-4">
          {[
            ["Images", String(stats.images)],
            ["Videos", String(stats.videos)],
            ["Favorites", String(stats.favorites)],
            ["Disk used", fmtBytes(stats.disk_bytes)],
          ].map(([label, value], index) => (
            <div key={label} className={`px-4 py-3 first:pl-0 ${index > 0 ? "border-l border-edge" : ""}`}>
              <dt className="label mb-0">{label}</dt>
              <dd className="technical mt-1 text-lg">{value}</dd>
            </div>
          ))}
        </dl>
      )}

      <div className="mt-8 grid items-start gap-10 lg:grid-cols-[180px_minmax(0,1fr)]">
        <nav className="sticky top-6 hidden border-l border-edge lg:block" aria-label="Settings sections">
          {[
            ["connections", "Connections"],
            ["presets", "Presets"],
            ["wildcards", "Wildcards"],
            ["files", "File metadata"],
            ["models", "Models"],
            ["access", "API access"],
            ["about", "About"],
          ].map(([id, label]) => (
            <a
              key={id}
              href={`#${id}`}
              className="block border-l-2 border-transparent px-4 py-2 text-xs text-muted hover:border-accent hover:text-ink"
            >
              {label}
            </a>
          ))}
        </nav>

        <div className="min-w-0 space-y-12">
          <SettingsSection
            id="connections"
            icon="cloud"
            title="Remote GPU connection"
            description="Connect the remote compute lane used by A100 image and video processes."
          >
            <p className="text-xs leading-relaxed text-muted">
              Run <code className="technical text-accent">make remote-gpu</code>, then copy{" "}
              <code className="technical text-accent">remote_gpu_filled.py</code> to GPU hardware you operate
              and run it there. Enter its public HTTPS URL below. The shared secret from{" "}
              <code className="technical">.env</code> is injected automatically. See the{" "}
              <a
                className="text-accent underline hover:text-ink"
                href="https://github.com/wizards-ecosystem/wizards-brush/blob/main/docs/remote-gpu.md"
              >
                Remote GPU guide
              </a>{" "}
              for deployment and network requirements.
            </p>
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <label className="label" htmlFor="remote-gpu-url">
                  Remote GPU URL
                </label>
                <input
                  id="remote-gpu-url"
                  className="input"
                  placeholder="https://….trycloudflare.com"
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                />
              </div>
              <div>
                <label className="label" htmlFor="remote-gpu-secret">
                  Shared secret
                </label>
                <input
                  id="remote-gpu-secret"
                  className="input"
                  type="password"
                  placeholder="Required again when the URL host changes"
                  value={secret}
                  onChange={(event) => setSecret(event.target.value)}
                />
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <Button variant="primary" icon="cloud" loading={saving} onClick={save}>
                {saving ? "Testing connection…" : "Save and test"}
              </Button>
              {msg && (
                <span
                  className={`flex items-center gap-2 text-sm ${msg.startsWith("Error") ? "text-danger" : "text-muted"}`}
                >
                  <Icon name={msg.startsWith("Error") ? "alert" : "check"} size={15} />
                  {msg}
                </span>
              )}
            </div>
          </SettingsSection>

          <SettingsSection
            id="presets"
            icon="bookmark"
            title={`Saved presets · ${presets.length}`}
            description="Prompt and style fragments saved from generator inspectors."
          >
            {presets.length === 0 ? (
              <EmptyState
                icon="bookmark"
                title="No saved presets"
                description="Use the bookmark control beside a generator prompt to keep language you want to reuse."
              />
            ) : (
              <div className="divide-y divide-edge border-y border-edge">
                {presets.map((preset) => (
                  <div key={preset.id} className="flex items-center justify-between gap-3 py-2.5">
                    <span className="min-w-0 truncate text-sm">
                      <span className="technical mr-3 text-[9px] uppercase text-muted">{preset.type}</span>
                      {preset.name}
                    </span>
                    <Button
                      variant="quiet"
                      size="sm"
                      className="text-danger"
                      icon="trash"
                      onClick={() => delPreset(preset.id)}
                    >
                      Delete
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </SettingsSection>

          <SettingsSection
            id="wildcards"
            icon="braces"
            title="Wildcards"
            description="Manage the local text lists used by the prompt engine."
          >
            <WildcardManager />
          </SettingsSection>

          <SettingsSection
            id="files"
            icon="info"
            title="File metadata & provenance"
            description="Choose what new media and future exports reveal when they leave this library."
          >
            <p className="text-xs leading-relaxed text-muted">
              Reproducibility settings include the prompt, seed, model, and adapters. Provenance is the
              separate IPTC declaration that AI contributed to the pixels.
            </p>
            <PrivacyToggle
              label="Embed generation settings"
              description="Lets The Wizard's Brush and A1111-compatible tools restore the generation setup."
              checked={data.embed_metadata ?? true}
              onChange={(value) => void saveExportSetting("embed_metadata", value)}
            />
            <PrivacyToggle
              label="Embed AI provenance"
              description="Adds the standard IPTC DigitalSourceType declaration without exposing the prompt."
              checked={data.embed_provenance ?? true}
              onChange={(value) => void saveExportSetting("embed_provenance", value)}
            />
          </SettingsSection>

          <SettingsSection
            id="models"
            icon="gpu"
            title="Model inventory"
            description="Read-only runtime configuration; edit model identifiers and tokens in the project-root .env file."
          >
            <div className="divide-y divide-edge border-y border-edge">
              {data.models?.map((model) => (
                <Row key={`${model.lane}:${model.kind}:${model.label}`} k={model.label} v={model.id} />
              ))}
              <Row
                k="Local quant"
                v={`${data.local?.quant} · offload ${data.local?.offload ? "on" : "off"}`}
              />
              <Row k="HF token" v={data.hf_token_hint || "(not set)"} />
              <Row k="Model cache" v={data.storage?.huggingface || "models/huggingface"} />
              <Row k="Tool weights" v={data.storage?.weights || "models/weights"} />
              <Row k="LoRAs" v={data.storage?.loras || "models/loras"} />
              <Row k="Generated data" v={data.storage?.output || "output"} />
              <Row k="Runtime/cache" v={data.storage?.runtime || ".runtime"} />
            </div>
            <Link to="/diagnosis" className="btn w-fit text-xs">
              <Icon name="info" size={15} /> Inspect readiness and hardware profile
            </Link>
          </SettingsSection>

          <SettingsSection
            id="access"
            icon="settings"
            title="API access"
            description="Store the optional API token in the active browser profile. Use make browser-dev or make browser to keep that profile inside the project."
          >
            <p className="text-xs leading-relaxed text-muted">
              When the backend has <code className="technical text-accent">API_TOKEN</code> set, enter the
              same token here. Leave it blank when authentication is disabled.
            </p>
            <div className="flex flex-col gap-3 sm:flex-row">
              <label className="min-w-0 flex-1">
                <span className="sr-only">API token</span>
                <input
                  className="input"
                  type="password"
                  placeholder="No browser token"
                  value={apiToken}
                  onChange={(event) => setApiToken(event.target.value)}
                />
              </label>
              <Button
                onClick={() => {
                  if (apiToken) localStorage.setItem("api_token", apiToken);
                  else localStorage.removeItem("api_token");
                  syncMediaToken(apiToken);
                  toast("API token saved — reloading", "success");
                  setTimeout(() => location.reload(), 400);
                }}
              >
                Save token
              </Button>
            </div>
          </SettingsSection>

          <SettingsSection
            id="about"
            icon="info"
            title="About The Wizard's Brush"
            description="Project version, documentation, licensing, and source."
          >
            <div className="divide-y divide-edge border-y border-edge">
              <Row k="Version" v={system?.version || "unknown"} />
              <Row k="Application license" v="Apache-2.0" />
            </div>
            <div className="flex flex-wrap gap-2">
              <a className="btn text-xs" href="https://github.com/wizards-ecosystem/wizards-brush">
                <Icon name="external" size={15} /> Source and documentation
              </a>
              <a
                className="btn text-xs"
                href="https://github.com/wizards-ecosystem/wizards-brush/blob/main/LICENSE"
              >
                <Icon name="external" size={15} /> Apache-2.0 license
              </a>
              <a className="btn text-xs" href="/third-party-notices.txt">
                <Icon name="info" size={15} /> Third-party notices
              </a>
            </div>
          </SettingsSection>
        </div>
      </div>
    </div>
  );
}

function PrivacyToggle({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 border-y border-edge py-3 select-none">
      <span>
        <span className="block text-sm text-ink/85">{label}</span>
        <span className="mt-0.5 block text-[11px] leading-relaxed text-muted">{description}</span>
      </span>
      <input
        type="checkbox"
        className="peer sr-only"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span
        className={`h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-focus ${checked ? "bg-accent" : "bg-edge"}`}
      >
        <span
          className={`block size-4 rounded-full bg-bg transition-transform ${checked ? "translate-x-4" : ""}`}
        />
      </span>
    </label>
  );
}

function SettingsSection({
  id,
  icon,
  title,
  description,
  children,
}: {
  id: string;
  icon: IconName;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-6 border-t border-edge pt-5">
      <div className="mb-5 flex items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-[7px] bg-panel2 text-accent">
          <Icon name={icon} size={18} />
        </span>
        <div>
          <h2 className="text-lg font-semibold">{title}</h2>
          <p className="mt-0.5 text-xs leading-relaxed text-muted">{description}</p>
        </div>
      </div>
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="grid gap-1 py-2.5 text-sm sm:grid-cols-[140px_minmax(0,1fr)]">
      <span className="text-muted">{k}</span>
      <span className="technical break-all text-xs text-ink sm:text-right">{v}</span>
    </div>
  );
}
