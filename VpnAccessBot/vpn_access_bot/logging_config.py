from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vpn_access_bot.correlation import get_correlation_id
from vpn_access_bot.telegram.context import get_bot_key

_SECRET_PATTERNS = [
    re.compile(r"(?i)(token|authorization|x-admin-token)(\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(?:vless|vmess|trojan|ss)://[^\s]+"),
    re.compile(r"(?i)([?&]token=)[^&\s]+"),
]


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)(token"):
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        elif pattern.pattern.startswith("(?i)([?&]"):
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class BoundedQueueHandler(logging.handlers.QueueHandler):
    def __init__(
        self,
        log_queue: queue.Queue[logging.LogRecord],
        emergency_handler: logging.Handler,
    ) -> None:
        super().__init__(log_queue)
        self._emergency_handler = emergency_handler
        self._dropped_records = 0
        self._counter_lock = threading.Lock()

    @property
    def dropped_records(self) -> int:
        with self._counter_lock:
            return self._dropped_records

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self._counter_lock:
                self._dropped_records += 1
            if record.levelno >= logging.ERROR:
                self._emergency_handler.handle(record)


class BoundedQueueListener(logging.handlers.QueueListener):
    def enqueue_sentinel(self) -> None:
        while True:
            try:
                self.queue.put_nowait(self._sentinel)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    continue
                if hasattr(self.queue, "task_done"):
                    self.queue.task_done()


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        record.bot_key = get_bot_key() or "-"
        return True


class JsonLineFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        message = redact_text(record.getMessage())
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "component": record.name,
            "event": getattr(record, "event", "log.message"),
            "correlationId": getattr(record, "correlation_id", "-"),
            "botKey": getattr(record, "bot_key", "-"),
            "message": message,
        }
        error_code = getattr(record, "error_code", None)
        if error_code:
            payload["errorCode"] = redact_text(str(error_code))
        if record.exc_info:
            payload["exceptionType"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class PermissionedTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o640)
        except OSError:
            pass
        return stream


@dataclass
class LoggingRuntime:
    listener: logging.handlers.QueueListener
    queue_handler: BoundedQueueHandler

    @property
    def dropped_records(self) -> int:
        return self.queue_handler.dropped_records

    def close(self) -> None:
        self.listener.stop()
        logging.shutdown()


def configure_logging(settings) -> LoggingRuntime:
    log_directory = Path(settings.log_directory)
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        os.chmod(log_directory, 0o750)
    except OSError:
        pass

    context_filter = ContextFilter()
    console = logging.StreamHandler()
    console.addFilter(context_filter)
    console.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s "
            "[correlation_id=%(correlation_id)s bot_key=%(bot_key)s]: %(message)s"
        )
    )

    file_handler = PermissionedTimedRotatingFileHandler(
        log_directory / "bot.jsonl",
        when="midnight",
        interval=1,
        backupCount=settings.log_retention_days,
        encoding="utf-8",
        utc=True,
        delay=True,
    )
    file_handler.addFilter(context_filter)
    file_handler.setFormatter(JsonLineFormatter("VpnAccessBot"))

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=10_000)
    queue_handler = BoundedQueueHandler(log_queue, console)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root.addHandler(queue_handler)

    listener = BoundedQueueListener(
        log_queue,
        console,
        file_handler,
        respect_handler_level=True,
    )
    listener.start()
    return LoggingRuntime(listener, queue_handler)
