import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const lora = (over: Record<string, unknown>) => ({
  family: "zimage",
  arch: "Z-Image",
  rank: 16,
  compatible: true,
  pickled: false,
  ...over,
});

const catalog = {
  items: [
    lora({
      name: "a",
      label: "Alpha Style",
      filename: "a.safetensors",
      path: "a.safetensors",
      size_bytes: 150e6,
    }),
    lora({
      name: "b",
      label: "Beta Style",
      filename: "b.safetensors",
      path: "b.safetensors",
      size_bytes: 2.5e9,
    }),
    lora({ name: "c", label: "Gamma", filename: "c.safetensors", path: "c.safetensors", size_bytes: 1e6 }),
  ],
  max_active: 2,
  weight_min: -2,
  weight_max: 2,
};

const catalogFor = { current: catalog as any };
vi.mock("../api/client", () => ({ api: { loras: () => Promise.resolve(catalogFor.current) } }));

// The component caches the catalogue at module scope; re-import per test.
async function load() {
  vi.resetModules();
  return (await import("./LoraPicker")).LoraPicker;
}

describe("LoraPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    catalogFor.current = catalog;
  });

  it("adds an adapter at weight 1.0", async () => {
    const LoraPicker = await load();
    const onChange = vi.fn();
    render(<LoraPicker value={[]} onChange={onChange} />);
    await userEvent.click(await screen.findByText(/Add LoRA/));
    await userEvent.click(screen.getByText("Alpha Style"));
    expect(onChange).toHaveBeenCalledWith([{ path: "a.safetensors", weight: 1.0 }]);
  });

  it("uses a sidecar's tested starting weight when present", async () => {
    catalogFor.current = {
      ...catalog,
      items: [
        lora({
          name: "a",
          label: "Alpha Style",
          filename: "a.safetensors",
          path: "a.safetensors",
          size_bytes: 150e6,
          recommended_weight: 0.45,
        }),
      ],
    };
    const LoraPicker = await load();
    const onChange = vi.fn();
    render(<LoraPicker value={[]} onChange={onChange} />);
    await userEvent.click(await screen.findByText(/Add LoRA/));
    await userEvent.click(screen.getByText("Alpha Style"));
    expect(onChange).toHaveBeenCalledWith([{ path: "a.safetensors", weight: 0.45 }]);
  });

  it("keeps SDXL text-encoder strength behind an advanced disclosure", async () => {
    catalogFor.current = {
      ...catalog,
      items: [
        lora({
          name: "a",
          label: "Alpha Style",
          filename: "a.safetensors",
          path: "a.safetensors",
          size_bytes: 150e6,
          family: "sdxl",
          arch: "Stable Diffusion XL",
        }),
      ],
    };
    const onChange = vi.fn();
    const LoraPicker = await load();
    render(
      <LoraPicker value={[{ path: "a.safetensors", weight: 0.9 }]} onChange={onChange} modelVariant="sdxl" />,
    );
    await userEvent.click(await screen.findByText(/Advanced · text encoder/));
    fireEvent.change(screen.getByLabelText("Text encoder weight for Alpha Style"), {
      target: { value: "0.35" },
    });
    expect(onChange).toHaveBeenCalledWith([{ path: "a.safetensors", weight: 0.9, te_weight: 0.35 }]);
  });

  it("enforces the backend's stack cap rather than its own number", async () => {
    const LoraPicker = await load();
    const onChange = vi.fn();
    const full = [
      { path: "a.safetensors", weight: 1 },
      { path: "b.safetensors", weight: 1 },
    ];
    render(<LoraPicker value={full} onChange={onChange} />);
    await waitFor(() => expect(screen.getByText(/Stack limit reached \(2\)/)).toBeInTheDocument());
    await userEvent.click(screen.getByText(/Add LoRA/));
    // an unselected entry is disabled at the cap
    expect(screen.getByRole("button", { name: /Gamma/ })).toBeDisabled();
  });

  it("removes an adapter", async () => {
    const LoraPicker = await load();
    const onChange = vi.fn();
    render(<LoraPicker value={[{ path: "a.safetensors", weight: 0.8 }]} onChange={onChange} />);
    await userEvent.click(await screen.findByLabelText("Remove Alpha Style"));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("flags a selection whose file has disappeared", async () => {
    const LoraPicker = await load();
    render(<LoraPicker value={[{ path: "deleted.safetensors", weight: 1 }]} onChange={vi.fn()} />);
    expect(await screen.findByText(/\(missing\)/)).toBeInTheDocument();
  });

  it("tells the user where to put files when there are none", async () => {
    // Uses the shared catalogFor indirection rather than vi.doMock: doMock is
    // NOT undone by resetModules, so an inline re-mock here silently applied to
    // every test after it and handed them all an empty catalogue.
    catalogFor.current = { ...catalog, items: [] };
    const LoraPicker = await load();
    render(<LoraPicker value={[]} onChange={vi.fn()} />);
    expect(await screen.findByText(/No LoRAs yet/)).toBeInTheDocument();
  });

  it("groups incompatible adapters instead of hiding them", async () => {
    // Hiding a file someone downloaded five minutes ago reads as a broken app,
    // not as a helpful filter.
    catalogFor.current = {
      ...catalog,
      items: [
        lora({ name: "a", label: "Fits", filename: "a.safetensors", path: "a.safetensors", size_bytes: 1e6 }),
        lora({
          name: "f",
          label: "Flux One",
          filename: "f.safetensors",
          path: "f.safetensors",
          size_bytes: 1e6,
          family: "flux",
          arch: "FLUX.1",
          compatible: false,
        }),
      ],
    };
    const LoraPicker = await load();
    render(<LoraPicker value={[]} onChange={vi.fn()} modelVariant="turbo" />);
    await userEvent.click(await screen.findByText(/Add LoRA/));

    expect(screen.getByText("Fits")).toBeInTheDocument();
    expect(screen.queryByText("Flux One")).not.toBeInTheDocument();

    await userEvent.click(screen.getByText(/Not for this model \(1\)/));
    expect(screen.getByText("Flux One")).toBeInTheDocument();
  });

  it("does not allow selecting an incompatible adapter", async () => {
    catalogFor.current = {
      ...catalog,
      items: [
        lora({
          name: "f",
          label: "Flux One",
          filename: "f.safetensors",
          path: "f.safetensors",
          size_bytes: 1e6,
          family: "flux",
          arch: "FLUX.1",
          compatible: false,
        }),
      ],
    };
    const onChange = vi.fn();
    const LoraPicker = await load();
    render(<LoraPicker value={[]} onChange={onChange} modelVariant="turbo" />);
    await userEvent.click(await screen.findByText(/Add LoRA/));
    await userEvent.click(screen.getByText(/Not for this model \(1\)/));
    await userEvent.click(screen.getByText("Flux One"));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("keeps full checkpoints visible but disabled in a separate group", async () => {
    catalogFor.current = {
      ...catalog,
      items: [
        lora({
          name: "checkpoint",
          label: "Whole checkpoint",
          filename: "checkpoint.safetensors",
          path: "checkpoint.safetensors",
          size_bytes: 6e9,
          is_adapter: false,
          selectable: false,
          unavailable_reason: "This safetensors file contains model weights, not a recognized adapter.",
        }),
      ],
    };
    const onChange = vi.fn();
    const LoraPicker = await load();
    render(<LoraPicker value={[]} onChange={onChange} />);
    await userEvent.click(await screen.findByText(/Add LoRA/));

    expect(screen.queryByText("Whole checkpoint")).not.toBeInTheDocument();
    await userEvent.click(screen.getByText(/Not adapters \(1\)/));
    const row = screen.getByRole("button", { name: /Whole checkpoint/ });
    expect(row).toBeDisabled();
    expect(row).toHaveAttribute("title", expect.stringContaining("not a recognized adapter"));
    await userEvent.click(row);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("treats a catalogue with no compatibility field as all-compatible", async () => {
    // An older backend that does not send `compatible` must not collapse the
    // whole library into "not for this model".
    catalogFor.current = {
      ...catalog,
      items: [
        {
          name: "a",
          label: "Legacy Entry",
          filename: "a.safetensors",
          path: "a.safetensors",
          size_bytes: 1e6,
        },
      ],
    };
    const LoraPicker = await load();
    render(<LoraPicker value={[]} onChange={vi.fn()} />);
    await userEvent.click(await screen.findByText(/Add LoRA/));
    expect(screen.getByText("Legacy Entry")).toBeInTheDocument();
    expect(screen.queryByText(/Not for this model/)).not.toBeInTheDocument();
  });

  it("shows what an adapter was built for", async () => {
    const LoraPicker = await load();
    render(<LoraPicker value={[]} onChange={vi.fn()} modelVariant="turbo" />);
    await userEvent.click(await screen.findByText(/Add LoRA/));
    expect(screen.getAllByText("Z-Image").length).toBeGreaterThan(0);
    expect(screen.getAllByText("r16").length).toBeGreaterThan(0);
  });
});
