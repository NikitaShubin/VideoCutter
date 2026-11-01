// Легенда таймлайна: смысл цветов должен объясняться на месте, а не только
// в справке. Свёрнута по умолчанию — не отнимает место у кадра и списка.
// Запуск: npm test (vitest run).
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TimelineLegend } from "./TimelineLegend";

describe("легенда таймлайна", () => {
  it("свёрнута по умолчанию", () => {
    const html = renderToStaticMarkup(<TimelineLegend />);
    expect(html).toContain("<details");
    expect(html).not.toContain("<details open");
    expect(html).toContain("Легенда таймлайна");
  });

  it("называет значения цветов словами", () => {
    const html = renderToStaticMarkup(<TimelineLegend />);
    expect(html).toContain("свободно");
    expect(html).toContain("фрагмент");
    expect(html).toContain("фрагмент с комментарием");
    expect(html).toContain("просмотрено");
    expect(html).toContain("текущий кадр");
  });
});
