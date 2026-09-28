import logging
import os
import time
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    if os.getenv('MEDREADY_FILE_LOGS') == '1':
        folder = Path(__file__).resolve().parents[1] / 'logs'
        folder.mkdir(exist_ok=True)
        handler = TimedRotatingFileHandler(folder / 'medready.log', when='midnight', backupCount=7, encoding='utf-8')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_time(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        started = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            get_logger(func.__module__).info('%s took %.3fs', func.__name__, time.monotonic() - started)
    return wrapped

