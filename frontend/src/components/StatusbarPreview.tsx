import { useEffect, useRef } from "react";
import { paintStatusbar, type ProgressLayers, type StatusbarScheme } from "../model/statusbar";
import type { Fragment } from "../types";

interface Props {
  totalFrames?: number;
  fragments?: Fragment[];
  position?: number;
  className?: string;
  /** Палитра канвы: стадии создания задачи вместо разметки. */
  scheme?: StatusbarScheme;
  /** Слоёный прогресс: заменяет таймлайн (фазы поверх фаз). */
  progress?: ProgressLayers;
}

// Мини-статус-бар «как если бы файл был открыт»: те же цвета и разметка,
// что в редакторе (единый источник — model/statusbar.ts). В списке служит
// фоном строки — визуальная память по состоянию workspace.
export function StatusbarPreview({
  totalFrames = 100,
  fragments = [],
  position = 0,
  className,
  scheme,
  progress,
}: Props) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    paintStatusbar(ctx, {
      width: canvas.width,
      height: canvas.height,
      totalFrames,
      fragments,
      position,
      keyPose: null,
      scheme,
      progress,
    });
  }, [totalFrames, fragments, position, scheme, progress]);

  return <canvas ref={ref} width={800} height={20} className={className} aria-hidden />;
}