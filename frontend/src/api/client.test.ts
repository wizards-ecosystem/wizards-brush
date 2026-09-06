import { afterEach, describe, expect, it, vi } from "vitest";
import { buildQuery, jobSocket, saveBrowserSession } from "./client";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
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
  it("migrates a v0.1.0 token once and removes its persistent copy", async () => {
    localStorage.setItem("api_token", "old token");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true, authenticated: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const cookieSetter = vi.spyOn(Document.prototype, "cookie", "set");
    vi.resetModules();

    const freshClient = await import("./client");
    expect(localStorage.getItem("api_token")).toBeNull();
    expect(cookieSetter.mock.calls.map(([value]) => value)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("wb_media_token=; Path=/files; SameSite=Strict; Max-Age=0"),
        expect.stringContaining("wb_ws_token=; Path=/api/jobs/ws; SameSite=Strict; Max-Age=0"),
      ]),
    );
    await freshClient.prepareBrowserSession();

    expect(fetchMock).toHaveBeenCalledWith("/api/auth/session", {
      method: "POST",
      headers: { "X-API-Token": "old token" },
    });
    expect(freshClient.authHeaders()).toEqual({});
  });

  it("exchanges the API token without writing it to browser storage", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true, authenticated: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await saveBrowserSession("secret value");

    expect(fetchMock).toHaveBeenCalledWith("/api/auth/session", {
      method: "POST",
      headers: { "X-API-Token": "secret value" },
    });
    expect(localStorage.getItem("api_token")).toBeNull();
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
    jobSocket(() => undefined);
    expect(opened).toMatch(/\/api\/jobs\/ws$/);
    expect(new URL(opened).search).toBe("");
  });
});
