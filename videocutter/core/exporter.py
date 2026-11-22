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


class ExportCancelled(RuntimeError):
    """Отмена экспорта пользователем (в т.ч. посреди фрагмента)."""


# Контейнеры, в которых копирование потока без пересжатия проверено:
# метки/индексы ключевых кадров читаются ffmpeg точно, а смена контейнера
# не требуется (webm/ogv получают .mp4 и потому копированию не подлежат).
_COPY_SAFE_EXTS = (".mp4", ".mov", ".m4v", ".mkv")


def _seconds(value: float) -> str:
    """Секунды для аргументов ffmpeg: фиксированная точность, без exp."""
    return f"{float(value):.6f}"


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
        nice: Optional[int] = None,
    ) -> None:
        self.source_video_file = source_video_file
        self.out_dir = out_dir

        source_dir, source_basename = os.path.split(source_video_file)
        source_name, source_ext = os.path.splitext(source_basename)
        self.target_prefix = os.path.join(out_dir, source_name)
        # Контейнеры, чей мультиплексор отвергает H.264 (WebM: только
        # VP8/VP9/AV1; OGG: только Theora/VP8): молча менять кодек —
        # значит менять качество и скорость всего пайплайна (CRF/preset
        # у каждого кодера свои), поэтому сохраняем проверенный
        # libx264-пайплайн и меняем только расширение на .mp4. Содержимое
        # нарезок — попиксельно то же, что выбиралось.
        if source_ext.lower() in (".webm", ".ogv", ".ogg"):
            self.target_suffix = ".mp4"
        else:
            self.target_suffix = source_ext
        self.remove_duplicates = remove_duplicates
        self.crf = crf
        self.preset = preset
        self.tune = tune
        self.nice = nice

    def _copy_allowed(self, target_file: str) -> bool:
        """Копирование потока допустимо: видео, не PNG, безопасный контейнер."""
        if target_file.lower().endswith(".png"):
            return False
        ext = os.path.splitext(self.source_video_file)[1].lower()
        return ext in _COPY_SAFE_EXTS

    def _build_copy_command(self, target_file: str,
                            copy_range: Tuple[float, Optional[float]]) -> List[str]:
        """Команда вырезания без пересжатия: -ss по ключевому кадру, -c copy.

        ``copy_range`` — (начало, конец-исключающий) в секундах; ``None``
        вместо конца — до конца файла. Диапазон обязан начинаться с
        ключевого кадра: иначе ffmpeg молча притянет предыдущий ключевой
        кадр и в нарезку попадут лишние кадры (это ловит сверка).
        """
        t0, t1 = copy_range
        t0 = float(t0)
        if t1 is not None:
            t1 = float(t1)
            if t1 <= t0:
                raise FFmpegError(
                    f"Некорректный диапазон копирования: {t0}..{t1}")
        cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            # Входной seek: для -c copy это переход к ключевому кадру.
            "-ss",
            _seconds(t0),
            "-i",
            self.source_video_file,
            "-y",
            "-map",
            "0:v:0",
            "-an",
        ]
        if t1 is not None:
            # Верхняя граница — начало следующего GOP: его ключевой кадр
            # не входит (ffmpeg режет по PTS пакета).
            cmd += ["-t", _seconds(t1 - t0)]
        cmd += [
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            target_file,
        ]
        if self.nice is not None:
            cmd = ["nice", "-n", str(int(self.nice))] + cmd
        return cmd

    def build_command(self, start: int, end: int, target_file: str,
                      frame_ts_range=None, copy_range=None) -> List[str]:
        """Собирает команду ffmpeg для вырезания кадров [start, end] (вкл.).

        Привязка — только к кадрам: ``start``/``end`` — индексы. Отбор идёт
        по ``frame_ts_range`` (секунды из индекса — метки именно этих кадров):
        неуязвим к сбоям счётчика ``n`` на аномальных файлах.
        ``between`` инклюзивен: ровно end-start+1.

        ``copy_range`` (секунды) включает копирование потока без пересжатия;
        допустимо только для видео в безопасном контейнере и диапазона,
        начинающегося с ключевого кадра. PNG и небезопасные контейнеры
        копирование игнорируют — остаётся проверенный libx264-путь.
        """
        if copy_range is not None and self._copy_allowed(target_file):
            return self._build_copy_command(target_file, copy_range)
        # Аргумент передаётся списком (subprocess), поэтому кавычки не нужны.
        # БЕЗ setpts: setpts=N/... съедает один кадр из выхлопа (доказано
        # матрицей: 5 отобранных -> 4 в файле; без setpts — ровно). Метки
        # непрерывны внутри диапазона, перенумерация не нужна; non-zero
        # старт для mp4 штатен (avoid_negative_ts страхует).
        if frame_ts_range is not None:
            t0, t1 = frame_ts_range
            select = f"select=between(t\\,{t0}\\,{t1})"
        else:
            select = f"select=between(n\\,{start}\\,{end})"
        if self.remove_duplicates:
            vf = f"mpdecimate,{select}"
        else:
            vf = select
        if target_file.lower().endswith(".png"):
            # Одиночный кадр — картинка без видеокодека: PNG lossless,
            # ровно один кадр, читается тем же ffprobe/ffmpeg-путём.
            cmd = [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                self.source_video_file,
                "-y",
                "-vf",
                vf,
                "-an",
                "-frames:v",
                "1",
                "-c:v",
                "png",
                target_file,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                self.source_video_file,
                "-y",
                "-avoid_negative_ts",
                "make_zero",
                "-vf",
                vf,
                "-an",
                "-vsync",
                "0",
                # Не декодировать хвост исходника после последнего нужного
                # кадра: верхняя граница выхода, а не источник истины —
                # точность кадров доказывает сверка.
                "-frames:v",
                str(end - start + 1),
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
        if self.nice is not None:
            cmd = ["nice", "-n", str(int(self.nice))] + cmd
        return cmd

    @staticmethod
    def _create_partial(path: str) -> None:
        """Создаёт скрытый частичный файл с правами 0600 до записи ffmpeg."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)

    def publish(self, partial_path: str) -> str:
        """Публикует проверенный частичный файл: 0644 + атомарный rename."""
        directory, name = os.path.split(partial_path)
        if not name.startswith("."):
            raise FFmpegError(f"Не частичный файл экспорта: {partial_path}")
        final_path = os.path.join(directory, name[1:])
        os.chmod(partial_path, 0o644)
        os.replace(partial_path, final_path)
        return final_path

    def _run_ffmpeg(self, cmd: List[str], cancelled,
                    fragment_ind: int) -> Tuple[int, str]:
        """Запускает ffmpeg с отменой и возвращает (код, stderr)."""
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True)
        try:
            while proc.poll() is None:
                if cancelled is not None and cancelled():
                    raise ExportCancelled(
                        f"Экспорт отменён на фрагменте {fragment_ind}")
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            _, stderr = proc.communicate()
        finally:
            # Подбираем процесс при любом исходе (мёртвый — no-op).
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
        return proc.returncode, stderr

    def extract_fragments(
        self,
        fragments: Sequence[Fragment],
        progress: Optional[Callable[[int, int, Fragment], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        frame_ts_ranges: Optional[Sequence[Optional[Tuple[float, float]]]] = None,
        copy_ranges: Optional[Sequence[Optional[Tuple[float, Optional[float]]]]] = None,
    ) -> List[str]:
        """Режет все фрагменты и возвращает список созданных файлов.

        :param fragments: список пар (start, end) индексов кадров.
        :param progress: callback(fragment_index (1-based), total, fragment).
        :param cancelled: callback () -> bool; True — убить текущий ffmpeg
            немедленно (не ждать границу фрагмента) и бросить ExportCancelled.
        :param frame_ts_ranges: опционально диапазоны меток кадров
            [(t0, t1), ...] параллельно fragments (None — отбор по счётчику n).
        :param copy_ranges: опционально секундные диапазоны [(t0, t1|None), ...]
            параллельно fragments для копирования без пересжатия. None в
            элементе — фрагмент пересжатием. Если копирование сорвалось,
            фрагмент автоматически пересжимается.
        :return: пути к скрытым частичным файлам (публикация — publish()).
        """
        if not shutil.which("ffmpeg"):
            raise FFmpegError("FFmpeg не найден в PATH")

        if frame_ts_ranges is not None and len(frame_ts_ranges) != len(fragments):
            raise FFmpegError("frame_ts_ranges не совпадает с fragments по длине")
        if copy_ranges is not None and len(copy_ranges) != len(fragments):
            raise FFmpegError("copy_ranges не совпадает с fragments по длине")

        created: List[str] = []
        total = len(fragments)
        for fragment_ind, (start, end) in enumerate(fragments, 1):
            # Одиночный кадр (start == end) — PNG: видео-контейнер с одним
            # кадром неудобен потребителю и хуже сверяется.
            suffix = ".png" if start == end else self.target_suffix
            final_file = (
                f"{self.target_prefix}_fragment_{fragment_ind}{suffix}"
            )
            partial_file = os.path.join(
                os.path.dirname(final_file), "." + os.path.basename(final_file))
            if progress:
                progress(fragment_ind, total, (start, end))

            frame_ts_range = frame_ts_ranges[fragment_ind - 1] if frame_ts_ranges is not None else None
            copy_range = copy_ranges[fragment_ind - 1] if copy_ranges is not None else None
            used_copy = (copy_range is not None
                         and self._copy_allowed(partial_file))
            self._create_partial(partial_file)
            cmd = self.build_command(start, end, partial_file,
                                     frame_ts_range=frame_ts_range,
                                     copy_range=copy_range)
            returncode, stderr = self._run_ffmpeg(
                cmd, cancelled, fragment_ind)
            if returncode != 0 and used_copy:
                # Копирование не удалось (необычный контейнер/поток) —
                # тот же фрагмент пересжатием: экспорт не должен падать
                # из-за необязательной оптимизации.
                self._create_partial(partial_file)
                cmd = self.build_command(
                    start, end, partial_file,
                    frame_ts_range=frame_ts_range)
                returncode, stderr = self._run_ffmpeg(
                    cmd, cancelled, fragment_ind)
            if returncode != 0:
                raise FFmpegError(
                    f"FFmpeg не смог обработать фрагмент {fragment_ind} "
                    f"(кадры {start}-{end}):\n{stderr[-2000:]}"
                )
            created.append(partial_file)

        return created
