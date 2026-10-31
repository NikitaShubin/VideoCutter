import type { ReactNode } from "react";

export type FragmentIconName =
  | "start"
  | "end"
  | "create"
  | "complete"
  | "delete"
  | "comment"
  | "undo"
  | "redo";

const PATHS: Record<FragmentIconName, ReactNode> = {
  // Начало: стрелка упирается в левую границу сегмента.
  start: (
    <>
      <path d="M5 4v16" />
      <path d="M20 12H9" />
      <path d="m13 8-4 4 4 4" />
    </>
  ),
  // Конец: зеркально началу.
  end: (
    <>
      <path d="M19 4v16" />
      <path d="M4 12h11" />
      <path d="m11 8 4 4-4 4" />
    </>
  ),
  create: (
    <>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </>
  ),
  complete: <path d="m5 12 5 5L20 7" />,
  delete: (
    <>
      <path d="M4 7h16" />
      <path d="M9 7V5h6v2" />
      <path d="M7 7l1 13h8l1-13" />
      <path d="M10 11v5" />
      <path d="M14 11v5" />
    </>
  ),
  comment: <path d="M4 5h16v11H9l-5 4V5z" />,
  undo: (
    <>
      <path d="M9 7 4 12l5 5" />
      <path d="M4 12h9a6 6 0 0 1 6 6" />
    </>
  ),
  redo: (
    <>
      <path d="m15 7 5 5-5 5" />
      <path d="M20 12h-9a6 6 0 0 0-6 6" />
    </>
  ),
};

/** Значки действий разметки: одни и те же в панели и в справке. */
export function FragmentIcon({ name }: { name: FragmentIconName }) {
  return (
    <svg
      className="fragment-icon"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
