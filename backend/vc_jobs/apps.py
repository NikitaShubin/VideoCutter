# -*- coding: utf-8 -*-
"""Запуск планировщика вместе с Django-процессом.

В проде gunicorn работает одним воркером, поэтому один планировщик на
процесс — это один хозяин очереди. В dev-режиме ``runserver`` запускает
два процесса (родитель-релоадер и ребёнок); стартуем только в ребёнке,
иначе два планировщика делили бы одни записи.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)


def repair_access_on_boot() -> None:
    """Идемпотентно чинит права старых exports/ (не блокирует запуск)."""
    try:
        import export_access
        import workspace

        export_access.repair_exports(
            workspace.WORKSPACE_ROOT,
            anchor=os.environ.get("VC_ACCESS_ANCHOR") or None)
    except Exception:  # noqa: BLE001 — диагностика не должна ронять сервис
        logger.exception("Не удалось починить права экспортированных файлов")


def should_autostart(argv=None, env=None) -> bool:
    argv = list(sys.argv if argv is None else argv)
    env = os.environ if env is None else env
    if env.get("VC_JOBS_AUTOSTART", "1") == "0":
        return False
    if any(arg in ("test", "pytest") for arg in argv):
        return False
    if "runserver" in argv and "--noreload" not in argv:
        return env.get("RUN_MAIN") == "true"
    return True


class VcJobsConfig(AppConfig):
    name = "vc_jobs"
    verbose_name = "Фоновые операции"

    def ready(self) -> None:
        if not should_autostart():
            return
        import vc_jobs

        try:
            vc_jobs.get_scheduler().start()
        except Exception:  # noqa: BLE001 — сервис должен подняться
            logger.exception("Не удалось запустить планировщик операций")
        threading.Thread(
            target=repair_access_on_boot,
            name="vc-export-access-repair", daemon=True).start()
