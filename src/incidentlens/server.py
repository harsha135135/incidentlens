"""The API/gateway process: the public listener (127.0.0.1:8088), the internal listener
(127.0.0.1:8089) and the action executor, sharing one connection pool.

On startup, before accepting work, unfinished actions from a previous run are marked UNKNOWN or
INTERRUPTED; nothing is redispatched (spec §8).

Run: PYTHONPATH=src uv run python -m incidentlens.server
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from urllib.parse import urlparse

import uvicorn
from psycopg_pool import ConnectionPool

from incidentlens import auth, evidence
from incidentlens.api import create_public_app
from incidentlens.config import INTERNAL_PORT, PUBLIC_PORT, load_settings
from incidentlens.executor import Executor
from incidentlens.internal_api import create_internal_app
from incidentlens.model_gateway import model_settings

log = logging.getLogger("incidentlens.server")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings, conninfo = test_overrides(load_settings())
    model = model_settings()
    secret_hash = auth.configured_secret_hash()
    pool = ConnectionPool(conninfo, min_size=1, max_size=8, kwargs={"autocommit": True}, open=True,
                          reconnect_timeout=5)
    with pool.connection() as conn:
        log.info("startup recovery: %s", evidence.recover_unfinished_actions(conn))
    replay = replay_bundles()
    if replay:
        log.warning("EVALUATION REPLAY MODE: tool actions are answered from frozen bundles in %s", replay)
    executor = Executor(settings, model, conninfo, replay)
    executor.start()
    public = uvicorn.Server(uvicorn.Config(create_public_app(pool, settings, secret_hash), host="127.0.0.1",
                                           port=PUBLIC_PORT,
                                           log_level="warning"))
    internal = uvicorn.Server(uvicorn.Config(create_internal_app(pool, executor.wake.set), host="127.0.0.1",
                                             port=INTERNAL_PORT,
                                             log_level="warning"))
    log.info("listening: public 127.0.0.1:%s, internal 127.0.0.1:%s", public.config.port, internal.config.port)
    try:
        asyncio.run(serve_both(public, internal))
    finally:
        executor.stop()
        pool.close()


def replay_bundles():
    """Evaluation replay mode (D51), accepted only with INCIDENTLENS_EVALUATION_MODE=1."""
    path = os.environ.get("INCIDENTLENS_REPLAY_BUNDLES")
    if not path:
        return None
    if os.environ.get("INCIDENTLENS_EVALUATION_MODE") != "1":
        raise RuntimeError("INCIDENTLENS_REPLAY_BUNDLES is accepted only with INCIDENTLENS_EVALUATION_MODE=1")
    from pathlib import Path
    bundles = Path(path).resolve()
    if not bundles.is_dir():
        raise RuntimeError(f"replay bundle directory {bundles} does not exist")
    return bundles


def test_overrides(settings):
    """Test mode only (INCIDENTLENS_TEST_MODE=1): another IncidentLens database (for example through a
    proxy the test can cut) and a loopback stand-in for Prometheus. Refused otherwise."""
    database = os.environ.get("INCIDENTLENS_APP_CONNINFO_OVERRIDE")
    prometheus = os.environ.get("INCIDENTLENS_TEST_PROMETHEUS_URL")
    if (database or prometheus) and os.environ.get("INCIDENTLENS_TEST_MODE") != "1":
        raise RuntimeError("test overrides are accepted only with INCIDENTLENS_TEST_MODE=1")
    if prometheus:
        if urlparse(prometheus).hostname != "127.0.0.1":
            raise RuntimeError("INCIDENTLENS_TEST_PROMETHEUS_URL must be a 127.0.0.1 address")
        settings = dataclasses.replace(settings, prometheus_url=prometheus)
    return settings, database or settings.app_conninfo


async def serve_both(public: uvicorn.Server, internal: uvicorn.Server) -> None:
    await asyncio.gather(public.serve(), internal.serve())


if __name__ == "__main__":
    main()
