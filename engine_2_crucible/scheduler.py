"""Maintenance job scheduling for IP4 Crucible — heartbeat-indexed."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    every_n_heartbeats: int
    runner: Callable[[], None]


def build_maintenance_jobs() -> list[ScheduledJob]:
    def validation_evolution() -> None:
        from engine_2_crucible.evolution import GeneticEvolution

        GeneticEvolution().execute_epoch()

    def resurrection() -> None:
        from engine_2_crucible.resurrection import main as resurrection_main

        resurrection_main()

    def janitor() -> None:
        from engine_2_crucible.data_janitor import DataJanitor

        DataJanitor().purge_stale_signals()

    return [
        ScheduledJob("validation_evolution", 100, validation_evolution),
        ScheduledJob("resurrection", 300, resurrection),
        ScheduledJob("janitor", 144, janitor),
    ]


def run_due_jobs(heartbeat_counter: int, jobs: list[ScheduledJob]) -> None:
    if heartbeat_counter <= 0:
        return
    for job in jobs:
        if heartbeat_counter % job.every_n_heartbeats != 0:
            continue
        logger.info(">>> Crucible maintenance: %s (heartbeat %d)", job.name, heartbeat_counter)
        try:
            job.runner()
        except Exception as exc:
            logger.error("Maintenance job %s failed: %s", job.name, exc)
