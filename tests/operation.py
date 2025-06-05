import asyncio
import random

SLEEP = 1  # up to SLEEP seconds

async def divide(x, y):
    print(f"DIV-thinking about {x} / {y}")
    await asyncio.sleep(SLEEP * random.random())
    print(f"DIV-returning {x} / {y}")
    return x / y



result_queue = asyncio.Queue(maxsize=1)
async def remote_callback(result: asyncio.Future, correlation_id):
    await result_queue.put(result)

async def divide_result_bad_callback_1(result):
    pass

async def divide_result_bad_callback_2(a, b, c):
    pass
