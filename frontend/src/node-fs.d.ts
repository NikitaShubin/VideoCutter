// Минимальные объявления для чтения файла из теста: @types/node в проекте
// нет, а тянуть его ради одного импорта не хочется.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
}

declare const process: { cwd(): string };
