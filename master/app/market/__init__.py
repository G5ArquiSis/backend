"""Lado comprador del mercado (rol D): libro de ofertas, bids y aceptación de swaps.

Importar el paquete registra sus handlers. Algunos extienden handlers de la E1 (`nack`) y de
la wallet (`market-closed`, `swap-settled`, `swap-rejected`), así que esos módulos se
importan primero, cualquiera sea el orden en que se cargue el paquete.
"""

from app import cycle_repository as _cycle_repository  # noqa: F401
from app.market import book, buyer
from app.wallet import eventos as _wallet_handlers  # noqa: F401

book.register()
buyer.register()
