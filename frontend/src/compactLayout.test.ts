// Короткий портрет (320×568): панель разметки не должна уезжать под сгиб.
// Без этих правил сетка 4×2 занимает 152 px и вся разметка оказывается ниже
// первого экрана — достать её можно только прокруткой. Правила живут в CSS,
// jsdom их не считает, поэтому держим контракт текстом таблицы стилей.
//
// Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

const css = readFileSync(`${process.cwd()}/src/styles.css`, "utf8");

/** Правила компактной раскладки: блок медиазапроса целиком. */
function compactRules(): string {
  const start = css.indexOf(
    "@media (orientation: portrait) and (max-width: 360px) and (max-height: 640px)",
  );
  expect(start).toBeGreaterThan(-1);
  const end = css.indexOf("@media", start + 10);
  return css.slice(start, end === -1 ? css.length : end);
}

describe("короткий портрет", () => {
  it("панель разметки — одна прокручиваемая строка, а не сетка", () => {
    const rules = compactRules();
    expect(rules).toMatch(/\.editor\.is-compact \.editor-workbench\s*\{[^}]*flex-wrap:\s*nowrap/);
    expect(rules).toMatch(/\.editor\.is-compact \.editor-workbench\s*\{[^}]*overflow-x:\s*auto/);
  });

  it("имя задачи — одна строка", () => {
    expect(compactRules()).toMatch(
      /\.editor\.is-compact \.pair-name\s*\{[^}]*-webkit-line-clamp:\s*1/,
    );
  });

  it("кадр отдаёт лишние пиксели контролам", () => {
    const rules = compactRules();
    expect(rules).toMatch(
      /\.editor\.is-compact \.video-stage\s*\{[^}]*flex:\s*1 1 auto/,
    );
    expect(rules).toMatch(
      /\.editor\.is-compact \.video-stage \.video-area\s*\{[^}]*aspect-ratio:\s*auto/,
    );
  });
});
