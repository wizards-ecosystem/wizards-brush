import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { VariantFinishingStep, VariantProcessor } from "../api/types";
import { DynamicControls } from "./DynamicControls";
import { FinishingStepsEditor, newStep } from "./FinishingStepsEditor";

const processors: VariantProcessor[] = [
  {
    name: "background_removal",
    label: "Remove background",
    description: "",
    available: true,
    unavailable_reason: null,
    options: [],
  },
  {
    name: "resize",
    label: "Resize to exact size",
    description: "",
    available: true,
    unavailable_reason: null,
    options: [
      { name: "width", label: "Width", type: "number", default: 1024, min: 1, max: 8192 },
      { name: "height", label: "Height", type: "number", default: 1024, min: 1, max: 8192 },
      { name: "mode", label: "Fit", type: "segmented", default: "contain", options: ["contain", "cover"] },
      {
        name: "background",
        label: "Padding",
        type: "select",
        default: "transparent",
        options: ["transparent", "#ffffff"],
      },
    ],
  },
  {
    name: "upscale",
    label: "Upscale (Real-ESRGAN)",
    description: "",
    available: false,
    unavailable_reason: "needs torch",
    options: [],
  },
];

function Harness({
  initial = [],
  onChange,
}: {
  initial?: VariantFinishingStep[];
  onChange: (s: unknown) => void;
}) {
  const [steps, setSteps] = useState<VariantFinishingStep[]>(initial);
  return (
    <FinishingStepsEditor
      steps={steps}
      processors={processors}
      onChange={(next) => {
        setSteps(next);
        onChange(next);
      }}
    />
  );
}

describe("FinishingStepsEditor", () => {
  it("adds a step with its declared defaults and edits its options", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    expect(screen.getByText(/kept as generated/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Add a finishing step"), "resize");
    expect(onChange).toHaveBeenLastCalledWith([
      { processor: "resize", width: 1024, height: 1024, mode: "contain", background: "transparent" },
    ]);
    const width = screen.getByLabelText("Resize to exact size: Width");
    await userEvent.clear(width);
    await userEvent.type(width, "512");
    expect(onChange.mock.lastCall?.[0][0].width).toBe(512);
  });

  it("keeps order meaningful: steps move, and removal keeps the rest", async () => {
    const onChange = vi.fn();
    render(
      <Harness initial={[{ processor: "background_removal" }, newStep(processors[1])]} onChange={onChange} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Move Resize to exact size earlier" }));
    expect(onChange.mock.lastCall?.[0].map((s: VariantFinishingStep) => s.processor)).toEqual([
      "resize",
      "background_removal",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Remove Resize to exact size" }));
    expect(onChange.mock.lastCall?.[0]).toEqual([{ processor: "background_removal" }]);
  });

  it("says what cannot run here instead of offering it", () => {
    render(<Harness initial={[{ processor: "upscale", scale: "2" }]} onChange={vi.fn()} />);
    const add = screen.getByLabelText("Add a finishing step");
    expect(Array.from(add.querySelectorAll("option")).map((o) => o.value)).not.toContain("upscale");
    expect(screen.getByText(/needs torch: this step will be skipped/)).toBeInTheDocument();
    expect(
      screen.getByText(/Not available here: Upscale \(Real-ESRGAN\) \(needs torch\)/),
    ).toBeInTheDocument();
  });

  it("is what a generator form renders for a finishing control", async () => {
    const onChange = vi.fn();
    render(
      <DynamicControls
        controls={[
          {
            name: "finish_steps",
            label: "Finishing steps",
            type: "finishing",
            default: [],
            processors,
          },
        ]}
        values={{ finish_steps: [] }}
        onChange={onChange}
        presets={null}
      />,
    );
    await userEvent.selectOptions(screen.getByLabelText("Add a finishing step"), "background_removal");
    expect(onChange).toHaveBeenCalledWith("finish_steps", [{ processor: "background_removal" }]);
  });
});
