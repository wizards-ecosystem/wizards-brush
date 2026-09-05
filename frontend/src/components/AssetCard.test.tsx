import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Asset } from "../api/types";
import { AssetCard } from "./AssetCard";

const asset: Asset = {
  id: 7,
  kind: "image",
  filename: "study.png",
  url: "/study.png",
  thumb_url: null,
  width: 1024,
  height: 1024,
  job_id: 1,
  generator: "txt2img",
  meta: { prompt: "A quiet lighthouse" },
  created_at: "2026-01-01T00:00:00Z",
  favorite: true,
  rating: 4,
  tags: [],
};

describe("AssetCard", () => {
  it("separates opening an asset from its accessible selection control", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    const select = vi.fn();
    render(<AssetCard asset={asset} onClick={open} selectable selected={false} onToggleSelect={select} />);

    await user.click(screen.getByRole("button", { name: /open image/i }));
    expect(open).toHaveBeenCalledWith(asset);
    expect(select).not.toHaveBeenCalled();

    const selection = screen.getByRole("button", { name: "Select asset" });
    expect(selection).toHaveAttribute("aria-pressed", "false");
    await user.click(selection);
    expect(select).toHaveBeenCalledWith(asset);
  });
});

describe("AssetCard signals", () => {
  const base = {
    id: 1,
    kind: "image" as const,
    filename: "a.png",
    url: "/f/a.png",
    thumb_url: "/t/a.jpg",
    width: 512,
    height: 512,
    job_id: 1,
    generator: "local",
    meta: {},
    created_at: "",
    favorite: false,
    rating: 0,
    tags: [],
  };

  it("badges an asset that has a pixel-identical twin", () => {
    render(<AssetCard asset={{ ...base, copies: 2 }} onClick={vi.fn()} />);
    expect(screen.getByTitle(/2 images .*identical/)).toBeInTheDocument();
  });

  it("shows no badge for a unique asset", () => {
    render(<AssetCard asset={{ ...base, copies: 1 }} onClick={vi.fn()} />);
    expect(screen.queryByTitle(/identical/)).not.toBeInTheDocument();
  });

  it("treats a missing copies field as unique", () => {
    // An older backend that does not send the field must not badge everything.
    render(<AssetCard asset={base} onClick={vi.fn()} />);
    expect(screen.queryByTitle(/copies/)).not.toBeInTheDocument();
  });

  it("flags an asset whose file has gone from disk", () => {
    render(<AssetCard asset={{ ...base, is_missing: true }} onClick={vi.fn()} />);
    expect(screen.getByTitle(/missing from disk/)).toBeInTheDocument();
  });
});
