"""Entry point: `python -m simple_apps`."""

import logging
import signal
import sys
import threading

from .config import Config, ConfigError
from .server import make_server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as err:
        logging.error("configuration error: %s", err)
        return 2
    server = make_server(config)

    def stop(signum, _frame):
        logging.info("received signal %d, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.info("simple-apps listening on :%d, data in %s", config.port, config.data_dir)
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
