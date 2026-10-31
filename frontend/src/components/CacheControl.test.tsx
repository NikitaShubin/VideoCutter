// Значок — не имя: 💾 и ✓ не должны читаться скринридером как «дискета» и
// «галочка». У кнопок есть словесные имена, сами значки скрыты.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CacheControl } from "./CacheControl";
import { getCache, setCache, type CacheState } from "../api";

vi.mock("../api", () => ({
  getCache: vi.fn(),
  setCache: vi.fn(),
}));

const state: CacheState = {
  caps: { gops: 512, mb: 4096 },
  usage: {
    gops: 12,
    gops_cap: 512,
    mb: 120,
    mb_cap: 4096,
    sources: 2,
  },
  tuning: { ncpu: 8, ram_mb: 16000, auto: {}, source: {} },
};

beforeEach(() => {
  vi.mocked(getCache).mockResolvedValue(state);
  vi.mocked(setCache).mockResolvedValue(state);
});

async function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<CacheControl />);
  });
  return {
    host,
    unmount: () => {
      act(() => root.unmount());
      host.remove();
    },
  };
}

describe("ручка кэша", () => {
  it("сводка названа словами, дискета скрыта", async () => {
    const { host, unmount } = await mount();
    const summary = host.querySelector<HTMLButtonElement>(
      'button[aria-label^="GOP-кэш"]',
    );
    expect(summary).toBeTruthy();
    expect(summary?.querySelector('[aria-hidden="true"]')?.textContent).toBe(
      "💾",
    );
    unmount();
  });

  it("кнопка применения лимитов названа словами, галочка скрыта", async () => {
    const { host, unmount } = await mount();
    act(() => {
      host.querySelector<HTMLButtonElement>('button[aria-label^="GOP-кэш"]')!.click();
    });
    const apply = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Применить лимиты"]',
    );
    expect(apply).toBeTruthy();
    expect(apply?.querySelector('[aria-hidden="true"]')?.textContent).toBe("✓");
    unmount();
  });
});
