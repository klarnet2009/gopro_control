from __future__ import annotations

import argparse
import logging
import os
from logging.handlers import RotatingFileHandler

import uvicorn

from .api.app import create_app
from .config import load_config
from .config_store import ConfigStore

# Env-var override for the config path. Lets ops point at a system-wide config
# (e.g. /etc/gopro-mgmt/config.yaml) without changing the start command.
_CONFIG_ENV_VAR = "GOPRO_MGMT_CONFIG"
_DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_LOG_BACKUPS = 5


def _configure_logging(level: str, log_file: str | None) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    handlers: list[logging.Handler] = []
    if log_file:
        # Rotate at 5 MiB, keep 5 generations — ~25 MiB cap on disk; suitable
        # for the long-running daemon usage shipped via start.sh.
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=_DEFAULT_LOG_MAX_BYTES,
                backupCount=_DEFAULT_LOG_BACKUPS,
                encoding="utf-8",
            )
        )
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=level.upper(),
        format=fmt,
        handlers=handlers,
        force=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="gopro-mgmt", description="GoPro multi-camera control panel")
    parser.add_argument(
        "--config",
        default=os.environ.get(_CONFIG_ENV_VAR, "config.yaml"),
        help=f"Path to YAML config file (env: {_CONFIG_ENV_VAR})",
    )
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path; when set, logs rotate at 5 MiB × 5 generations.",
    )
    args = parser.parse_args()

    _configure_logging(args.log_level, args.log_file)

    cfg = load_config(args.config)
    store = ConfigStore(args.config)
    app = create_app(cfg, config_store=store)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
