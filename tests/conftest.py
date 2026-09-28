"""Reusa las fixtures de master (client, session, database) para el test E2E.

pytest solo aplica un conftest.py a los tests bajo su propio directorio o
subdirectorios, así que este archivo en tests/ no vería las fixtures definidas
en master/tests/conftest.py por herencia automática. La alternativa - copiar
client/session/database acá - duplicaría exactamente lo que este mismo encargo
pide no duplicar, así que en cambio se importa ese módulo por ruta de archivo
(master/ no es un paquete instalable: no tiene __init__.py) y se reexportan sus
fixtures con los mismos nombres.
"""

import importlib.util
import sys
from pathlib import Path

_master_conftest_path = Path(__file__).resolve().parent.parent / "master" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("master_tests_conftest", _master_conftest_path)
_master_conftest = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _master_conftest
_spec.loader.exec_module(_master_conftest)

app = _master_conftest.app
client = _master_conftest.client
database = _master_conftest.database
session = _master_conftest.session
