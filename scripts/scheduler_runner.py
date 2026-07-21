from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import boot_freshness_catchup, boot_stock_meta, init_scheduler


HEARTBEAT_PATH = Path(os.environ.get("QI_SCHEDULER_HEARTBEAT", "/tmp/quantinvest_scheduler_heartbeat"))
HEARTBEAT_INTERVAL = int(os.environ.get("QI_SCHEDULER_HEARTBEAT_INTERVAL", "30"))


def write_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(str(time.time()), encoding="ascii")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    logging.getLogger("scheduler_runner").info("starting QuantInvest scheduler service")
    boot_stock_meta()
    scheduler = init_scheduler()
    boot_freshness_catchup()
    try:
        while True:
            if not scheduler.running:
                raise RuntimeError("APScheduler stopped unexpectedly")
            write_heartbeat()
            time.sleep(HEARTBEAT_INTERVAL)
    finally:
        HEARTBEAT_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
