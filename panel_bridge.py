import asyncio
import threading

_bot = None
_loop = None
_lock = threading.Lock()


def init_bridge(bot, loop=None):
    global _bot, _loop
    with _lock:
        _bot = bot
        _loop = loop or asyncio.get_running_loop()


def is_ready():
    return _bot is not None and _loop is not None and _loop.is_running()


def get_bot():
    return _bot


def run_coroutine(coro, timeout=20):
    if not is_ready():
        raise RuntimeError("Botul Discord nu este încă pregătit pentru acțiuni din panel.")
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)
