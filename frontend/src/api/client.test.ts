import { afterEach, describe, expect, it, vi } from "vitest";
import { buildQuery, jobSocket, syncMediaToken } from "./client";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("buildQuery", () => {
  it("drops undefined, null and empty values", () => {
    expect(buildQuery({ a: 1, b: undefined, c: null, d: "", e: "x" })).toBe("a=1&e=x");
  });

  it("stringifies booleans and numbers", () => {
    expect(buildQuery({ favorite: true, min_rating: 3 })).toBe("favorite=true&min_rating=3");
  });

  it("URL-encodes values", () => {
    expect(buildQuery({ q: "a b&c" })).toBe("q=a+b%26c");
  });
});

describe("browser authentication", () => {
  it("writes separate path-scoped cookies for media and live events", () => {
    const setter = vi.spyOn(Document.prototype, "cookie", "set");
    syncMediaToken("secret value");
    expect(setter.mock.calls.map(([value]) => value)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("wb_media_token=secret%20value; Path=/files; SameSite=Strict"),
        expect.stringContaining("wb_ws_token=secret%20value; Path=/api/jobs/ws; SameSite=Strict"),
      ]),
    );
  });

  it("never puts the API token in the WebSocket URL", () => {
    let opened = "";
    class FakeWebSocket {
      binaryType = "";
      onmessage: ((event: MessageEvent) => void) | null = null;
      constructor(url: string | URL) {
        opened = String(url);
      }
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    localStorage.setItem("api_token", "do-not-log-me");
    jobSocket(() => undefined);
    expect(opened).toMatch(/\/api\/jobs\/ws$/);
    expect(opened).not.toContain("do-not-log-me");
  });
});
