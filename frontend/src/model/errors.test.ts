import { describe, expect, it } from "vitest";
import { failureReason } from "./errors";

describe("причина сбоя для пользователя", () => {
  it("сетевую ошибку браузера называет по-русски", () => {
    expect(failureReason(new TypeError("Failed to fetch"))).toBe(
      "нет связи с сервером",
    );
  });

  it("сообщение сервера оставляет как есть", () => {
    expect(failureReason(new Error("Фрагмент пересекается"))).toBe(
      "Фрагмент пересекается",
    );
  });

  it("пустую ошибку не оставляет пустой", () => {
    expect(failureReason(null)).toBe("неизвестная ошибка");
  });
});
