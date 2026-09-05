import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { ZoomableImage } from "./ZoomableImage";

const style = (el: HTMLElement) => el.getAttribute("style") || "";

describe("ZoomableImage", () => {
  it("starts fitted at 1x", () => {
    render(<ZoomableImage src="/a.png" alt="a" />);
    expect(style(screen.getByAltText("a"))).toContain("scale(1)");
  });

  it("zooms in on wheel up and never below fit", () => {
    const { container } = render(<ZoomableImage src="/a.png" alt="a" />);
    const box = container.firstChild as HTMLElement;

    fireEvent.wheel(box, { deltaY: -500, clientX: 100, clientY: 100 });
    const zoomed = style(screen.getByAltText("a"));
    expect(zoomed).not.toContain("scale(1)");

    // scrolling back out clamps at fit rather than inverting the image
    fireEvent.wheel(box, { deltaY: 5000, clientX: 100, clientY: 100 });
    expect(style(screen.getByAltText("a"))).toContain("scale(1)");
  });

  it("offers a way back to fit once zoomed", () => {
    const { container } = render(<ZoomableImage src="/a.png" alt="a" />);
    fireEvent.wheel(container.firstChild as HTMLElement, {
      deltaY: -800,
      clientX: 50,
      clientY: 50,
    });
    const fit = screen.getByLabelText("Reset zoom");
    fireEvent.click(fit);
    expect(style(screen.getByAltText("a"))).toContain("scale(1)");
  });

  it("double-click toggles between fit and zoomed", () => {
    const { container } = render(<ZoomableImage src="/a.png" alt="a" />);
    const box = container.firstChild as HTMLElement;
    fireEvent.doubleClick(box, { clientX: 10, clientY: 10 });
    expect(style(screen.getByAltText("a"))).toContain("scale(2)");
    fireEvent.doubleClick(box, { clientX: 10, clientY: 10 });
    expect(style(screen.getByAltText("a"))).toContain("scale(1)");
  });

  it("resets when the image changes", () => {
    /* Arrowing through the gallery while zoomed would otherwise land on an
       arbitrary crop of the next image. */
    const { container, rerender } = render(<ZoomableImage src="/a.png" alt="a" />);
    fireEvent.doubleClick(container.firstChild as HTMLElement, { clientX: 10, clientY: 10 });
    expect(style(screen.getByAltText("a"))).toContain("scale(2)");

    rerender(<ZoomableImage src="/b.png" alt="b" />);
    expect(style(screen.getByAltText("b"))).toContain("scale(1)");
  });
});
