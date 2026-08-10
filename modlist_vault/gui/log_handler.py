"""Routes util.logger records into the GUI's log panel, even from worker
threads - Qt signal/slot connections across threads are queued automatically,
so this is the safe way to get logging.Handler callbacks (which can fire from
any thread) onto the Qt main thread without a manual queue+poll loop."""

import logging

from PySide6.QtCore import QObject, Signal


class QtLogHandler(logging.Handler, QObject):
    record_logged = Signal(str)

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.record_logged.emit(self.format(record))
        except Exception:
            self.handleError(record)
