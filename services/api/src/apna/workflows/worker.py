"""Temporal worker processes.

    python -m apna.workflows.worker            # default queue: workflows + core activities
    python -m apna.workflows.worker --browser  # browser queue: Playwright activities (separate container)
"""

from __future__ import annotations

import argparse
import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from apna.core.config import get_settings
from apna.core.logging import configure_logging, log
from apna.workflows import activities
from apna.workflows.definitions import (
    BROWSER_QUEUE,
    DEFAULT_QUEUE,
    PaymentWorkflow,
    PriceWatchWorkflow,
    ReminderWorkflow,
    TaskWorkflow,
)


def build_workers(client: Client, *, default: bool = True, browser: bool = False) -> list[Worker]:
    activities.set_temporal_client(client)
    workers = []
    if default:
        workers.append(Worker(client, task_queue=DEFAULT_QUEUE, workflows=[TaskWorkflow, ReminderWorkflow, PaymentWorkflow, PriceWatchWorkflow],
                              activities=activities.DEFAULT_ACTIVITIES))
    if browser:
        # Browser activities are long and resource-heavy: low concurrency per worker.
        workers.append(Worker(client, task_queue=BROWSER_QUEUE, activities=activities.BROWSER_ACTIVITIES,
                              max_concurrent_activities=2))
    return workers


async def main(browser: bool, both: bool) -> None:
    configure_logging()
    s = get_settings()
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    workers = build_workers(client, default=not browser or both, browser=browser or both)
    log.info("worker_started", queues=[w.task_queue for w in workers])
    await asyncio.gather(*(w.run() for w in workers))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--browser", action="store_true", help="run only the browser task queue")
    p.add_argument("--all", action="store_true", help="run both queues in one process (local dev)")
    a = p.parse_args()
    asyncio.run(main(a.browser, a.all))
