import type { Project, VideoPair } from "../types";

/** Снимок состояния, снятый с сервиса в один момент. */
export interface SyncSnapshot {
  pairs: VideoPair[];
  projects: Project[];
}

function taskTitle(pair: VideoPair): string {
  return pair.name || pair.id;
}

function projectTitle(project: Project): string {
  return project.name || project.id;
}

function hasCuts(pair: VideoPair): boolean {
  return pair.export?.state === "done";
}

/**
 * Расхождения между двумя снимками: что изменилось на диске, пока сервис
 * смотрел в другую сторону. Сравниваем только то, что видно как состояние:
 * состав задач и проектов и наличие нарезок. Счётчики кадров и прочее — шум.
 */
export function diffSync(before: SyncSnapshot, after: SyncSnapshot): string[] {
  const changes: string[] = [];

  const beforeTasks = new Map(before.pairs.map((pair) => [pair.id, pair]));
  const afterTasks = new Map(after.pairs.map((pair) => [pair.id, pair]));

  for (const [id, pair] of afterTasks) {
    const old = beforeTasks.get(id);
    if (!old) {
      changes.push(`Появилась задача «${taskTitle(pair)}»`);
      continue;
    }
    if (hasCuts(old) && !hasCuts(pair)) {
      changes.push(`У задачи «${taskTitle(pair)}» пропали нарезки`);
    } else if (!hasCuts(old) && hasCuts(pair)) {
      changes.push(`У задачи «${taskTitle(pair)}» появились нарезки`);
    }
  }
  for (const [id, pair] of beforeTasks) {
    if (!afterTasks.has(id)) {
      changes.push(`Задача «${taskTitle(pair)}» исчезла`);
    }
  }

  const beforeProjects = new Map(before.projects.map((p) => [p.id, p]));
  const afterProjects = new Map(after.projects.map((p) => [p.id, p]));
  for (const [id, project] of afterProjects) {
    if (!beforeProjects.has(id)) {
      changes.push(`Появился проект «${projectTitle(project)}»`);
    }
  }
  for (const [id, project] of beforeProjects) {
    if (!afterProjects.has(id)) {
      changes.push(`Проект «${projectTitle(project)}» исчез`);
    }
  }

  return changes;
}
