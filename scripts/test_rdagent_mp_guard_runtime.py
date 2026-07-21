import os
from pathlib import Path
import sys
import time


RDAGENT_ROOT = Path(os.environ.get("RDAGENT_ROOT", r"C:\rdagent"))
sys.path.insert(0, str(RDAGENT_ROOT))

from rdagent.core.utils import RDAgentException, multiprocessing_wrapper  # noqa: E402


def _square(value):
    return value * value


def _exit_worker(_value):
    os._exit(17)


def main():
    assert multiprocessing_wrapper([(_square, (2,)), (_square, (3,))], n=2) == [4, 9]

    started = time.monotonic()
    try:
        multiprocessing_wrapper([(_exit_worker, (0,)), (_square, (4,))], n=2)
    except RDAgentException as exc:
        assert "worker exited" in str(exc)
    else:
        raise AssertionError("worker loss did not abort multiprocessing_wrapper")
    assert time.monotonic() - started < 20


if __name__ == "__main__":
    main()
