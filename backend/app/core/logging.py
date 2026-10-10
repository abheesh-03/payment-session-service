"""Logging setup for the ``app`` logger hierarchy.

Records are written as one JSON object per line. Fields passed via
``extra=`` are included as top-level keys. Uvicorn keeps its own loggers.
"""

import json
import logging
from datetime import UTC, datetime

_HANDLER_NAME = "app-json"
# Attributes every LogRecord has; anything else came from ``extra=``.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        )
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach the JSON handler to ``app``; safe to call more than once."""
    logger = logging.getLogger("app")
    logger.setLevel(level)
    if any(h.get_name() == _HANDLER_NAME for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    # Do not also emit through the root logger (avoids duplicate lines).
    logger.propagate = False
