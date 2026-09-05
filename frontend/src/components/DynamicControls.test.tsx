import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Control, Presets } from "../api/types";
import { DynamicControls, overriddenHidden } from "./DynamicControls";

const controls: Control[] = [
  {
    name: "quality",
    label: "Quality",
    type: "segmented",
    default: "Standard",
    options: ["Draft", "Standard", "Custom"],
  },
  {
    name: "steps",
    label: "Steps",
    type: "slider",
    default: 9,
    min: 1,
    max: 30,
    step: 1,
    section: "Advanced",
    show_if: { field: "quality", equals: "Custom" },
  },
  { name: "speed_mode", label: "Speed mode", type: "toggle", default: false },
  {
    name: "engine",
    label: "Engine",
    type: "select",
    default: "wan",
    options: ["wan", "hunyuan"],
    show_if: { field: "speed_mode", equals: [false, "off"] },
  },
];

describe("DynamicControls", () => {
  it("hides show_if controls until the condition matches", () => {
    const { rerender } = render(
      <DynamicControls
        controls={controls}
        values={{ quality: "Standard", speed_mode: false }}
        onChange={() => {}}
        presets={null}
      />,
    );
    // steps hidden (Advanced section not even shown since its only member is hidden)
    expect(screen.queryByText("Steps")).toBeNull();

    rerender(
      <DynamicControls
        controls={controls}
        values={{ quality: "Custom", speed_mode: false }}
        onChange={() => {}}
        presets={null}
      />,
    );
    // Advanced section now has a visible member — expand it
    fireEvent.click(screen.getByText(/Advanced/));
    expect(screen.getByText("Steps")).toBeInTheDocument();
  });

  it("supports array-valued show_if.equals", () => {
    render(
      <DynamicControls
        controls={controls}
        values={{ quality: "Standard", speed_mode: false }}
        onChange={() => {}}
        presets={null}
      />,
    );
    expect(screen.getByText("Engine")).toBeInTheDocument();
  });

  it("does not show a child whose own condition matches under a hidden parent", () => {
    const nested: Control[] = [
      { name: "finish", label: "Finish", type: "select", default: "none", options: ["none", "custom"] },
      {
        name: "post_detail",
        label: "Auto detail",
        type: "toggle",
        default: false,
        section: "Advanced",
        show_if: { field: "finish", equals: "custom" },
      },
      {
        name: "detail_prompt",
        label: "Detail prompt",
        type: "textarea",
        default: "",
        section: "Advanced",
        show_if: { field: "post_detail", equals: true },
      },
    ];
    render(
      <DynamicControls
        controls={nested}
        values={{ finish: "none", post_detail: true }}
        onChange={() => {}}
        presets={null}
      />,
    );
    expect(screen.queryByText(/Advanced controls/)).toBeNull();
    expect(screen.queryByText("Detail prompt")).toBeNull();
  });

  it("emits changes from a segmented control", () => {
    const onChange = vi.fn();
    render(
      <DynamicControls
        controls={controls}
        values={{ quality: "Standard", speed_mode: false }}
        onChange={onChange}
        presets={null}
      />,
    );
    fireEvent.click(screen.getByText("Draft"));
    expect(onChange).toHaveBeenCalledWith("quality", "Draft");
    expect(screen.getByRole("button", { name: "Standard" })).toHaveAttribute("aria-pressed", "true");
  });

  it("explains the currently selected model option", () => {
    const model: Control = {
      name: "model_variant",
      label: "Model",
      type: "segmented",
      default: "turbo",
      options: ["turbo", "quality"],
      option_hints: {
        turbo: "Fast distilled model.",
        quality: "Real CFG with a longer sampling schedule.",
      },
    };
    const { rerender } = render(
      <DynamicControls
        controls={[model]}
        values={{ model_variant: "turbo" }}
        onChange={() => {}}
        presets={null}
      />,
    );
    expect(screen.getByText("Fast distilled model.")).toBeInTheDocument();
    rerender(
      <DynamicControls
        controls={[model]}
        values={{ model_variant: "quality" }}
        onChange={() => {}}
        presets={null}
      />,
    );
    expect(screen.getByText("Real CFG with a longer sampling schedule.")).toBeInTheDocument();
  });

  it("exposes toggle state without relying on colour", () => {
    render(
      <DynamicControls
        controls={controls}
        values={{ quality: "Standard", speed_mode: false }}
        onChange={() => {}}
        presets={null}
      />,
    );
    expect(screen.getByRole("checkbox", { name: "Speed mode" })).not.toBeChecked();
  });

  it("narrows a control label and range from another selection", () => {
    const dependent: Control[] = [
      { name: "engine", label: "Engine", type: "select", default: "wan", options: ["wan", "ltx"] },
      {
        name: "num_frames",
        label: "Length (frames · 4n+1)",
        type: "slider",
        default: 49,
        min: 25,
        max: 121,
        step: 4,
        overrides_by: {
          field: "engine",
          map: { ltx: { label: "Length (frames)", min: 9, max: 129, step: 1 } },
        },
      },
    ];
    render(
      <DynamicControls
        controls={dependent}
        values={{ engine: "ltx", num_frames: 30 }}
        onChange={() => {}}
        presets={null}
      />,
    );
    const slider = screen.getByRole("slider", { name: "Length (frames)" });
    expect(slider).toHaveAttribute("min", "9");
    expect(slider).toHaveAttribute("max", "129");
    expect(slider).toHaveAttribute("step", "1");
  });
});

const tiered: Control[] = [
  { name: "prompt", label: "Prompt", type: "textarea", default: "", tier: "basic" },
  { name: "guidance", label: "Guidance", type: "slider", default: 1, min: 0, max: 6, step: 0.1 },
  {
    name: "sampler",
    label: "Sampler",
    type: "select",
    default: "default",
    options: ["default"],
    section: "Advanced",
  },
];

describe("DynamicControls simple mode", () => {
  it("shows only basic-tier controls and drops the Advanced disclosure", () => {
    render(<DynamicControls controls={tiered} values={{}} onChange={() => {}} presets={null} simple />);
    expect(screen.getByText("Prompt")).toBeInTheDocument();
    expect(screen.queryByText("Guidance")).toBeNull();
    expect(screen.queryByText(/Advanced/)).toBeNull();
  });

  it("shows everything in full mode", () => {
    render(<DynamicControls controls={tiered} values={{}} onChange={() => {}} presets={null} />);
    expect(screen.getByText("Guidance")).toBeInTheDocument();
    expect(screen.getByText(/Advanced/)).toBeInTheDocument();
  });

  it("falls back to the full form when nothing declares a tier", () => {
    // An untiered generator has nothing to simplify; filtering would leave an
    // empty panel above the Generate button.
    render(<DynamicControls controls={controls} values={{}} onChange={() => {}} presets={null} simple />);
    expect(screen.getByText("Quality")).toBeInTheDocument();
    expect(screen.getByText("Speed mode")).toBeInTheDocument();
  });
});

describe("overriddenHidden", () => {
  it("reports hidden controls that no longer hold their default", () => {
    const off = overriddenHidden(tiered, { prompt: "x", guidance: 1, sampler: "default" });
    expect(off).toEqual([]);

    const on = overriddenHidden(tiered, { prompt: "x", guidance: 6, sampler: "default" });
    expect(on.map((c) => c.name)).toEqual(["guidance"]);
  });

  it("reports nothing for an untiered generator, which Simple never filters", () => {
    expect(overriddenHidden(controls, { quality: "Custom", steps: 30 })).toEqual([]);
  });

  it("does not call an app-applied dependent default a hidden override", () => {
    const modelAware = [
      {
        name: "model_variant",
        label: "Model",
        type: "select" as const,
        default: "turbo",
        options: ["turbo", "quality"],
        tier: "basic",
      },
      {
        name: "guidance",
        label: "Guidance",
        type: "slider" as const,
        default: 1,
        defaults_by: { field: "model_variant", map: { turbo: 1, quality: 4 } },
      },
    ];
    expect(overriddenHidden(modelAware, { model_variant: "quality", guidance: 4 })).toEqual([]);
    expect(
      overriddenHidden(modelAware, { model_variant: "quality", guidance: 6 }).map((c) => c.name),
    ).toEqual(["guidance"]);
  });

  it("shows a constrained control at the effective value and disables it", () => {
    render(
      <DynamicControls
        controls={[
          { name: "speed_mode", label: "Lightning", type: "toggle", default: false, tier: "basic" },
          {
            name: "guidance",
            label: "Guidance",
            type: "slider",
            default: 4,
            min: 0,
            max: 12,
            constrained_by: {
              field: "speed_mode",
              equals: true,
              value: 1,
              reason: "Lightning fixes this value.",
            },
          },
        ]}
        values={{ speed_mode: true, guidance: 7 }}
        onChange={() => {}}
        presets={null}
      />,
    );
    expect(screen.getByRole("slider", { name: "Guidance" })).toBeDisabled();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("Lightning fixes this value.")).toBeInTheDocument();
  });
});

const presetPayload: Presets = {
  negative: [],
  prompt: [
    { id: "portrait", category: "People", label: "Portrait starter", text: "an environmental portrait" },
    { id: "poster", category: "Design", label: "Poster starter", text: "a clean graphic poster" },
  ],
  settings: [
    {
      id: "explore",
      label: "Explore ×4",
      description: "Four drafts",
      requires: ["quality", "batch"],
      values: { quality: "Draft", batch: 4 },
    },
  ],
  style_profiles: [],
  aspects: [],
  quality_tiers: [],
  hints: { guidance_turbo: "Turbo-specific help", guidance_local: "Fallback help" },
  user: [],
};

describe("creative presets", () => {
  it("groups prompt starters and inserts their natural-language text", () => {
    const onChange = vi.fn();
    render(
      <DynamicControls
        controls={[{ name: "prompt", label: "Prompt", type: "textarea", default: "", styles: true }]}
        values={{ prompt: "a ceramic fox" }}
        onChange={onChange}
        presets={presetPayload}
      />,
    );

    expect(screen.getByText("People")).toBeInTheDocument();
    expect(screen.getByText("Design")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Portrait starter/ }));
    expect(onChange).toHaveBeenCalledWith("prompt", "a ceramic fox, an environmental portrait");
  });

  it("applies only relevant workflow setting bundles", () => {
    const onChange = vi.fn();
    render(
      <DynamicControls
        controls={[
          {
            name: "quality",
            label: "Quality",
            type: "segmented",
            default: "Standard",
            options: ["Draft", "Standard"],
          },
          { name: "batch", label: "Batch", type: "slider", default: 1, min: 1, max: 8, step: 1 },
        ]}
        values={{ quality: "Standard", batch: 1 }}
        onChange={onChange}
        presets={presetPayload}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Explore ×4" }));
    expect(onChange).toHaveBeenCalledWith("quality", "Draft");
    expect(onChange).toHaveBeenCalledWith("batch", 4);
  });

  it("selects model-dependent setting help", () => {
    render(
      <DynamicControls
        controls={[
          {
            name: "model_variant",
            label: "Model",
            type: "select",
            default: "turbo",
            options: ["turbo"],
          },
          {
            name: "guidance",
            label: "Guidance",
            type: "slider",
            default: 0,
            min: 0,
            max: 8,
            step: 0.1,
            hint_key: "guidance_local",
            hint_keys_by: { field: "model_variant", map: { turbo: "guidance_turbo" } },
          },
        ]}
        values={{ model_variant: "turbo", guidance: 0 }}
        onChange={() => {}}
        presets={presetPayload}
      />,
    );

    expect(screen.getByText("Turbo-specific help")).toBeInTheDocument();
  });
});
