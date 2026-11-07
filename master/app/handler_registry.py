"""Registro compartido de handlers, extensible sin editar el dispatcher."""

APPLIED = "applied"

HANDLERS = {}


def register_handler(message_type: str, handler):

    if message_type in HANDLERS and HANDLERS[message_type] is not handler:
        raise ValueError(f'Handler ya registrado: {message_type}')

    HANDLERS[message_type] = handler

    return handler


def extend_handler(message_type: str, step):
    """Agrega `step` después del handler ya registrado para `message_type`, en la misma
    transacción.

    Sirve cuando dos módulos necesitan el mismo mensaje: por ejemplo, `swap-settled` lo aplica
    la wallet a los saldos (rol B) y además cierra el bid que lo originó (rol D). `step` recibe
    `(session, message, now, settings, outcome)`, donde `outcome` es lo que devolvió el handler
    anterior (o None si no había), y devuelve su propio outcome. El resultado es APPLIED si
    cualquiera de los dos aplicó algo; si no, el del handler anterior.
    """
    previous = HANDLERS.get(message_type)

    async def chained(session, message, now, settings):
        outcome = await previous(session, message, now, settings) if previous else None
        own = await step(session, message, now, settings, outcome)
        if APPLIED in (outcome, own):
            return APPLIED
        return outcome if outcome is not None else own

    HANDLERS[message_type] = chained
    return chained
