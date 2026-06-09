"""``python -m sandbox_orchestrator`` entrypoint.

Boots uvicorn with the env-driven :class:`Config`. The Helm Deployment runs
exactly this.
"""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import Config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config.from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
