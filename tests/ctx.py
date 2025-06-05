import aio_pika
import asyncio
from threading import Thread


class Context:
    def __init__(self):
        self.rmq_connection = None
        self.loop = asyncio.new_event_loop()

        def _loop_thread_target():
            self.loop.run_forever()
            self.loop.close()

        self._loop_thread = Thread(target=_loop_thread_target, daemon=True)
        self._loop_thread.start()

    async def get_rmq_con(self):
        if self.rmq_connection is None:
            self.rmq_connection = await aio_pika.connect_robust()
        return self.rmq_connection

    def run(self, coro) -> asyncio.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)