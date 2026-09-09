"""Чистое ядро VideoCutter: покадровое чтение, отбор фрагментов, экспорт.

Не зависит от способа ввода/вывода (порты IVideoSource / IFragmentStore),
поэтому может работать автономно ИЛИ встраиваться в VideoDataOps.
"""

from .model import CircleInd, Fragment, FragmentList
from .ports import IVideoSource, IFragmentStore
from .video_reader import VideoReader
from .fragment_editor import FragmentEditor
from .statusbar import draw_statusbar
from .exporter import Exporter, FFmpegError

__all__ = [
    "CircleInd",
    "Fragment",
    "FragmentList",
    "IVideoSource",
    "IFragmentStore",
    "VideoReader",
    "FragmentEditor",
    "draw_statusbar",
    "Exporter",
    "FFmpegError",
]
