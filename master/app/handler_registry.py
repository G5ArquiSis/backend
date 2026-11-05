"""Registro compartido de handlers, extensible sin editar el dispatcher."""
HANDLERS = {}


def register_handler(message_type: str, handler):

    if message_type in HANDLERS and HANDLERS[message_type] is not handler:
        raise ValueError(f'Handler ya registrado: {message_type}')

    HANDLERS[message_type] = handler

    return handler
