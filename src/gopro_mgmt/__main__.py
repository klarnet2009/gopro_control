from __future__ import annotations

import argparse
import logging

import uvicorn

from .api.app import create_app
from .config import load_config
from .config_store import ConfigStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="gopro-mgmt", description="GoPro multi-camera control panel")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    cfg = load_config(args.config)
    store = ConfigStore(args.config)
    app = create_app(cfg, config_store=store)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
