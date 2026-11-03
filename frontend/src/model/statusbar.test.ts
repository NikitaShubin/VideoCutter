// Маркер текущего кадра: на длинном таймлайне он должен читаться поверх
// цветных фрагментов, а не сливаться с ними.
import { describe, expect, it } from "vitest";
import { statusbarOps, type StatusbarState } from "./statusbar";

function state(overrides: Partial<StatusbarState> = {}): StatusbarState {
  return {
    width: 800,
    height: 36,
    totalFrames: 100,
    fragments: [],
    position: 50,
    ...overrides,
  };
}

function whiteMarkers(s: StatusbarState) {
  return statusbarOps(s).filter((op) => op.fill === "#ffffff");
}

describe("маркер текущего кадра", () => {
  it("в таймлайне — 2px, чтобы не теряться среди фрагментов", () => {
    const markers = whiteMarkers(state());
    expect(markers).toHaveLength(1);
    expect(markers[0].w).toBe(2);
  });

  it("в режиме прогресса — тоже 2px", () => {
    const markers = whiteMarkers(
      state({
        progress: { base: "#111111", fill: "#222222", frac: 0.5 },
      }),
    );
    expect(markers).toHaveLength(1);
    expect(markers[0].w).toBe(2);
  });
});
