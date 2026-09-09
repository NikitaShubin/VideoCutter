"""Адаптеры ввода/вывода для VideoCutter.

Ядро (videocutter.core) не знает, откуда кадры и куда фрагменты. Здесь —
конкретные реализации портов:
- ``local_source`` — чтение кадров из локального файла через OpenCV;
- ``file_store`` — хранение фрагментов в текстовом файле (.txt).
"""

from .local_source import LocalVideoSource
from .file_store import FileFragmentStore

__all__ = ["LocalVideoSource", "FileFragmentStore"]
