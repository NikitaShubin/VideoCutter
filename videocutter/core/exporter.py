"""Экспорт фрагментов видеопоследовательности через FFmpeg.

Перенос логики десктопного ``Backend.extract_fragments``: для каждого фрагмента
[start, end] вырезается соответствующий диапазон кадров. В отличие от исходной
версии, используется ``subprocess`` (а не ``os.system``) с контролем кода
возврата, а прогресс отдаётся через callback (GUI-независимо).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

from .model import Fragment


class FFmpegError(RuntimeError):
    """Возникает, когда FFmpeg завершился с ошибкой при экспорте фрагмента."""


class Exporter:
    def __init__(
        self,
        source_video_file: str,
        out_dir: str,
        *,
        remove_duplicates: bool = False,
        crf: int = 15,
        preset: str = "slow",
        tune: str = "animation",
    ) -> None:
        self.source_video_file = source_video_file
        self.out_dir = out_dir

        source_dir, source_basename = os.path.split(source_video_file)
        source_name, source_ext = os.path.splitext(source_basename)
        self.target_prefix = os.path.join(out_dir, source_name)
        self.target_suffix = source_ext
        self.remove_duplicates = remove_duplicates
        self.crf = crf
        self.preset = preset
        self.tune = tune

    def build_command(self, start: int, end: int, target_file: str) -> List[str]:
        """Собирает команду ffmpeg для вырезания кадров [start, end] (вкл.).

        Индексы — в пространстве видимых (декодируемых) кадров источника:
        ``select`` нумерует кадры, дошедшие до фильтра, а битые access unit
        кадра не дают и номер не занимают — поэтому такие кадры пропускаются
        сами, без дублей соседей и пустых вставок. ``mpdecimate`` по умолчанию
        выключен: он выкидывает кадры и сдвигает нумерацию, ломая соответствие
        индексов с превью.
        """
        # Аргумент передаётся списком (subprocess), поэтому кавычки не нужны.
        # select=between(n,start,end+1) — end+1 включается включительно в диапазон.
        select = f"select=between(n\\,{start}\\,{end + 1})"
        if self.remove_duplicates:
            vf = f"mpdecimate,setpts=N/FRAME_RATE/TB,{select},setpts=PTS-STARTPTS"
        else:
            vf = f"{select},setpts=N/FRAME_RATE/TB"
        return [
            "ffmpeg",
            "-i",
            self.source_video_file,
            "-y",
            "-avoid_negative_ts",
            "make_zero",
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            self.preset,
            "-crf",
            str(self.crf),
            "-tune",
            self.tune,
            target_file,
        ]

    def extract_fragments(
        self,
        fragments: Sequence[Fragment],
        progress: Optional[Callable[[int, int, Fragment], None]] = None,
    ) -> List[str]:
        """Режет все фрагменты и возвращает список созданных файлов.

        :param fragments: список пар (start, end) индексов кадров.
        :param progress: callback(fragment_index (1-based), total, fragment).
        :return: пути к созданным файлам.
        """
        if not shutil.which("ffmpeg"):
            raise FFmpegError("FFmpeg не найден в PATH")

        created: List[str] = []
        total = len(fragments)
        for fragment_ind, (start, end) in enumerate(fragments, 1):
            target_file = (
                f"{self.target_prefix}_fragment_{fragment_ind}{self.target_suffix}"
            )
            if progress:
                progress(fragment_ind, total, (start, end))

            cmd = self.build_command(start, end, target_file)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise FFmpegError(
                    f"FFmpeg не смог обработать фрагмент {fragment_ind} "
                    f"(кадры {start}-{end}):\n{result.stderr[-2000:]}"
                )
            created.append(target_file)

        return created
