"""
run.py — supervised entry point.

Запускает main.py в бесконечном цикле с экспоненциальным backoff:
если бот упал — ждём 5/10/20/30 сек и стартуем снова. Логи краша — в bot.log.

Используется как ENTRYPOINT в Docker или как `python run.py` локально, чтобы
бот гарантированно работал 24/7 без человеческого вмешательства.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG_FILE = Path(os.getenv("BOT_LOG_FILE", "bot.log"))
MAX_BACKOFF = 60  # cap

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ run-supervisor │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("supervisor")

_should_stop = False


def _handle_signal(signum, frame):
    global _should_stop
    _should_stop = True
    logger.info(f"Signal {signum} received, stopping supervisor...")


for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, _handle_signal)


def main():
    backoff = 5
    attempt = 0
    while not _should_stop:
        attempt += 1
        logger.info(f"Запуск main.py (попытка #{attempt})")
        start_ts = time.time()
        try:
            with open(LOG_FILE, "ab", buffering=0) as logf:
                logf.write(
                    f"\n=== supervisor: starting attempt #{attempt} at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
                )
                proc = subprocess.Popen(
                    [sys.executable, "main.py"],
                    stdout=logf, stderr=subprocess.STDOUT,
                )

            while not _should_stop:
                ret = proc.poll()
                if ret is not None:
                    break
                time.sleep(1)

            if _should_stop:
                logger.info("Stopping child process...")
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

            ret = proc.returncode
            uptime = time.time() - start_ts
            logger.warning(f"main.py завершился (code={ret}, uptime={uptime:.1f}s)")

            # Если бот проработал >5 мин — сбрасываем backoff
            if uptime > 300:
                backoff = 5
            else:
                backoff = min(MAX_BACKOFF, backoff * 2)

            logger.info(f"Перезапуск через {backoff}s...")
            for _ in range(backoff):
                if _should_stop:
                    break
                time.sleep(1)
        except Exception:
            logger.exception("supervisor: unexpected error")
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)

    logger.info("Supervisor завершён")


if __name__ == "__main__":
    main()
