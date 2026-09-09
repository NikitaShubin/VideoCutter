"""Точная копия оригинального ``Backend`` из PyVideoCutter (main.py) без Qt.

Используется для реплей-теста поведения фрагментов: одинаковые
последовательности «нажатий» применяются к этому классу и к
``FragmentEditor``/JS ``FragmentModel``, и результат сравнивается.

Единственные отличия от оригинала:
- ``save``/``load`` пишут во временный каталог, переданный в ``out_dir``;
- ``errorbox`` в ветке ``delete`` (ambiguous) заменена заглушкой ``print``.

Вся логика ``add/new_start/new_end/delete/undo/redo`` скопирована 1-в-1.
"""

from __future__ import annotations

import copy
import os
from typing import List


def _errorbox(msg: str, title: str) -> None:
    # Заглушка вместо QtDialogHelper.errorbox.
    print(f"[errorbox] {title}: {msg}")


class BackendLegacy:
    def __init__(self, total_frames, source_video_file, out_dir):
        self.fragments: List[List[int]] = []
        self.total_frames = total_frames
        self.source_video_file = source_video_file

        source_dir, source_basename = os.path.split(source_video_file)
        source_name, source_ext = os.path.splitext(source_basename)

        self.fragments_file = self.target_prefix = (
            os.path.join(out_dir, source_name + "_fragments.txt")
        )
        self.target_prefix = os.path.join(out_dir, source_name)
        self.target_suffix = source_ext

        os.makedirs(out_dir, exist_ok=True)
        self.load()
        self.hist_pos = 0
        self.init_hist()

    def init_hist(self):
        if os.path.isfile(self.fragments_file):
            self.load()
        else:
            self.fragments = []
            self.save()
        self.history = [copy.deepcopy(self.fragments)]
        self.hist_pos = 0

    def update_hist(self, sync=True):
        self.history = self.history[: self.hist_pos + 1] + [
            copy.deepcopy(self.fragments)
        ]
        self.hist_pos = len(self.history) - 1
        if sync:
            self.save()

    def undo(self, sync=True):
        if self.hist_pos:
            self.hist_pos -= 1
            self.fragments = copy.deepcopy(self.history[self.hist_pos])
            if sync:
                self.save()
            return True
        else:
            return False

    def redo(self, sync=True):
        next_hist_pos = self.hist_pos + 1
        if next_hist_pos == len(self.history):
            return False
        else:
            self.hist_pos = next_hist_pos
            self.fragments = copy.deepcopy(self.history[self.hist_pos])
            if sync:
                self.save()
            return True

    def save(self):
        with open(self.fragments_file, "w") as f:
            for start, end in self.fragments:
                f.write(f"{start} {end}\n")

    def load(self):
        self.fragments = []
        if os.path.isfile(self.fragments_file):
            with open(self.fragments_file, "r") as f:
                for line in f.readlines():
                    fragment = list(map(int, line.split(" ")))
                    self.fragments.append(fragment)

    def check(self, position):
        return 0 <= position < self.total_frames

    def start_frame2sb_axis(self, width, frame):
        return int(round((width - 1) * frame / self.total_frames)) if self.total_frames else 0

    def end_frame2sb_axis(self, width, frame):
        return self.start_frame2sb_axis(width, frame) + 1

    def draw_statusbar(self, width, height, position=0, select_range=None):
        import numpy as np

        sb = np.zeros((height, width, 3), np.uint8)
        sb[:, :, 1] = 255  # зелёный фон

        for start_frame, end_frame in self.fragments:
            start = self.start_frame2sb_axis(width, start_frame)
            end = self.end_frame2sb_axis(width, end_frame)
            sb[:, start:end, 2] = 255
            sb[:, start:end, 1] = 0

        if select_range is not None:
            start = self.start_frame2sb_axis(width, select_range[0])
            end = self.end_frame2sb_axis(width, select_range[1])
            sb[:, start:end, 0] = 255

        return sb

    def add(self, fragment):
        new_start, new_end = fragment
        new_ind = 0
        for ind, (start, end) in enumerate(self.fragments):
            if new_start > end:
                new_ind = ind + 1
            elif new_end < start:
                new_ind = ind
                break
            else:
                return False

        self.fragments = self.fragments[:new_ind] + [list(fragment)] + self.fragments[new_ind:]
        self.update_hist()
        return True

    def new_start(self, position):
        for ind, (start, end) in enumerate(self.fragments):
            if end >= position:
                self.fragments[ind] = [position, end]
                break
        else:
            self.fragments.append([position, self.total_frames - 1])
        self.update_hist()
        return True

    def new_end(self, position):
        for ind, (start, end) in reversed(list(enumerate(self.fragments))):
            if start <= position:
                self.fragments[ind] = [start, position]
                break
        else:
            self.fragments.append([0, position])
        self.update_hist()
        return True

    def delete(self, position):
        if len(self.fragments) == 0:
            return False

        point = [position, position]
        if point in self.fragments:
            self.fragments.remove(point)
            self.update_hist()
            return True
        else:
            fragments2del = [_ for _ in self.fragments if position in _]

            if len(fragments2del) == 1:
                self.fragments.remove(fragments2del[0])
                self.update_hist()
                return True

            elif len(fragments2del) == 2:
                _errorbox(
                    "Текущий кадр находится на стыке двух фрагментов. "
                    "Переместите курсор и повторите действие.",
                    "Ошибка",
                )
                return False

            elif len(fragments2del) == 0:
                if position < self.fragments[0][0]:
                    self.fragments[0][0] = 0
                    self.update_hist()
                    return True

                elif position > self.fragments[-1][1]:
                    self.fragments[-1][1] = self.total_frames - 1
                    self.update_hist()
                    return True

                import numpy as np

                fragments = np.array(self.fragments).flatten()
                for ind in range(len(fragments) - 1):
                    if fragments[ind] < position < fragments[ind + 1]:
                        fragments = np.delete(fragments, [ind, ind + 1])
                        fragments = fragments.reshape(-1, 2)
                        self.fragments = []
                        for fragment in fragments:
                            self.fragments.append(list(map(int, fragment)))
                        self.update_hist()
                        return True
                else:
                    raise ValueError("Ошибка логики программы!")

            else:
                raise ValueError("Ошибка в списках фрагментов!")

    def selected_frames(self):
        return sum([end - start for start, end, *_ in self.fragments])

    def get_all_key_frames(self):
        key_frames = set()
        for start, end in self.fragments:
            key_frames.add(start)
            key_frames.add(end)
        return sorted(key_frames)