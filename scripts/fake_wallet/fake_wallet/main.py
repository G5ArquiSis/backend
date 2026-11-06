"""Punto de entrada: `uvicorn fake_wallet.main:app`. La configuración viene del entorno."""

import logging

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
