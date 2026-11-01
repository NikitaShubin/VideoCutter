import { describe, expect, it } from "vitest";
import { predictRotateGain, ROTATE_MIN_GAIN } from "./rotateHint";

// Телефон 360×740: после поворота — 740×360, рейка 2×44 (108 px),
// полоса транспорта 81 px.
const phone = { viewportWidth: 360, viewportHeight: 740 };

describe("прогноз выигрыша от поворота телефона", () => {
  it("16:9 в ландшафте заметно шире портрета — подсказка уместна", () => {
    const p = predictRotateGain({
      ...phone,
      frameRatio: 16 / 9,
      portraitFrameWidth: 360,
    });
    // (740 − 8 − 108 − 12) и (360 − 81) × 16/9 — решает высота: 496 px.
    expect(p.landscapeWidth).toBeCloseTo((360 - 81) * (16 / 9), 1);
    expect(p.gain).toBeGreaterThanOrEqual(ROTATE_MIN_GAIN);
    expect(p.worthRotating).toBe(true);
  });

  it("кадру 3:2 поворот даёт меньше четверти — подсказки нет", () => {
    const p = predictRotateGain({
      ...phone,
      frameRatio: 1.5,
      portraitFrameWidth: 360,
    });
    expect(p.gain).toBeLessThan(ROTATE_MIN_GAIN);
    expect(p.worthRotating).toBe(false);
  });

  it("квадратный кадр в ландшафте меньше, чем в портрете", () => {
    const p = predictRotateGain({
      ...phone,
      frameRatio: 1,
      portraitFrameWidth: 360,
    });
    expect(p.landscapeWidth).toBeLessThan(360);
    expect(p.worthRotating).toBe(false);
  });

  it("узкий экран с широкой рейкой съедает выигрыш", () => {
    // 320×640 → ландшафт 640×320: рейка 4×44 (208 px) оставляет 412 px
    // ширины, а высота (239 px) при 1.6 даёт лишь 382 px.
    const p = predictRotateGain({
      frameRatio: 1.6,
      portraitFrameWidth: 320,
      viewportWidth: 320,
      viewportHeight: 640,
    });
    expect(p.landscapeWidth).toBeCloseTo(239 * 1.6, 1);
    expect(p.worthRotating).toBe(false);
  });

  it("пока кадр не измерен, подсказка не показывается", () => {
    const p = predictRotateGain({
      ...phone,
      frameRatio: 16 / 9,
      portraitFrameWidth: 0,
    });
    expect(p.gain).toBe(0);
    expect(p.worthRotating).toBe(false);
  });
});
