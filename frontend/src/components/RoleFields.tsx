import type { ReactNode } from "react";
import { Hint } from "./Hint";

interface Props {
  /** Роль строки: источник или превью. */
  role: "source" | "preview";
  /** Имя файла роли (как в списке задач). */
  name: string;
  /** Файл играет обе роли (отдельного превью нет). */
  shared: boolean;
  /** Роль сломана: файл не читается. */
  broken: boolean;
  /** Короткая пометка о поломке. */
  hint: string;
  /** Полный текст ошибки (в подсказке по имени файла). */
  error?: string;
  /** Действия роли («Заменить…», «Убрать»). */
  actions?: ReactNode;
}

/** Строка роли в панели ✎: имя файла, действия и подсветка поломки.
 *
 * Битая роль видна сразу: строка подсвечена, имя файла показано вместо
 * «(то же видео)», а рядом — короткая пометка, что файл не читается.
 * Полная причина — в подсказке по имени файла.
 */
export function RoleField({ role, name, shared, broken, hint, error, actions }: Props) {
  const title = role === "source" ? "Источник" : "Превью";
  const shown = broken ? name : shared ? "(то же видео)" : name;
  return (
    <div className={broken ? "role-field role-bad" : "role-field"}>
      <b>{title}</b>
      {broken ? (
        <Hint className="role-name role-name-bad" text={error || hint || "Файл не читается"}>
          {shown}
        </Hint>
      ) : (
        <span className="role-name">{shown}</span>
      )}
      {broken && <span className="role-bad-hint">{hint}</span>}
      {actions}
    </div>
  );
}
