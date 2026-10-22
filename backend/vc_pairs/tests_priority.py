# -*- coding: utf-8 -*-
"""Тесты приоритета процесса (priority.py).

Сервис не резервирует ядра: он понижает свой nice, чтобы системные задачи
получали процессор первыми, а свободные ядра доставались декодированию.

Прогон:  cd backend && python manage.py test vc_pairs
"""

import os
import subprocess
import sys
import unittest.mock as mock

from django.test import SimpleTestCase

import priority

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NiceFromEnvTests(SimpleTestCase):
    def test_default_value(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VC_NICE", None)
            self.assertEqual(priority.nice_from_env(), priority.DEFAULT_NICE)

    def test_value_from_env(self):
        with mock.patch.dict(os.environ, {"VC_NICE": "7"}):
            self.assertEqual(priority.nice_from_env(), 7)

    def test_broken_value_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"VC_NICE": "не число"}):
            self.assertEqual(priority.nice_from_env(), priority.DEFAULT_NICE)

    def test_value_clamped_to_range(self):
        with mock.patch.dict(os.environ, {"VC_NICE": "25"}):
            self.assertEqual(priority.nice_from_env(), 19)


class ApplyLowPriorityTests(SimpleTestCase):
    def test_explicit_value_is_used(self):
        with mock.patch.object(priority.os, "setpriority") as sp:
            priority.apply_low_priority(5)
        sp.assert_called_once_with(os.PRIO_PROCESS, 0, 5)

    def test_broken_explicit_value_falls_back_to_default(self):
        with mock.patch.object(priority.os, "setpriority") as sp:
            priority.apply_low_priority("не число")
        sp.assert_called_once_with(os.PRIO_PROCESS, 0, priority.DEFAULT_NICE)

    def test_current_nice_falls_back_to_zero(self):
        with mock.patch.object(priority.os, "getpriority",
                               side_effect=OSError("нет прав")):
            self.assertEqual(priority.current_nice(), 0)

    def test_process_nice_is_lowered(self):
        """Реальный эффект: nice процесса растёт (понижается приоритет)."""
        script = (
            "import os, priority;"
            "before = os.getpriority(os.PRIO_PROCESS, 0);"
            "after = priority.apply_low_priority();"
            "print(before, after)"
        )
        env = dict(os.environ, VC_NICE="7")
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=BACKEND_DIR, env=env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before, after = (int(x) for x in proc.stdout.split())
        # Понизить приоритет (nice вверх) можно всегда; обратно — нет прав.
        self.assertGreaterEqual(after, before)
        self.assertEqual(after, max(before, 7))

    def test_failure_does_not_break_startup(self):
        with mock.patch.dict(os.environ, {"VC_NICE": "5"}), \
                mock.patch.object(priority.os, "setpriority",
                                  side_effect=OSError("нет прав")):
            value = priority.apply_low_priority()
        self.assertEqual(value, priority.current_nice())


class ServiceStartupTests(SimpleTestCase):
    def test_wsgi_lowers_process_priority(self):
        """Сервис понижает приоритет при старте (wsgi — точка входа gunicorn)."""
        import importlib

        import videocutter_dj.wsgi as wsgi

        with mock.patch.object(priority, "apply_low_priority") as applied:
            importlib.reload(wsgi)
        applied.assert_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
