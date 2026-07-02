import logging
import json

log = logging.getLogger(__name__)

_handlers: dict[str, list] = {}


def on(event_name: str):
    """Decorator to register an event handler."""
    def decorator(fn):
        _handlers.setdefault(event_name, []).append(fn)
        return fn
    return decorator


def emit(event_name: str, payload: dict):
    """Fire event - runs all registered handlers synchronously."""
    log.info(json.dumps({"action": event_name, **payload}))
    for handler in _handlers.get(event_name, []):
        try:
            handler(payload)
        except Exception as e:
            log.error("Event handler %s for %s failed: %s", handler.__name__, event_name, e)
