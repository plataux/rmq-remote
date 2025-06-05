import remote

from typing import Any, Generator
import asyncio

import pytest
from ctx import Context

import operation

@pytest.fixture(scope="session")
def ctx() -> Generator[Context, Any, None]:
    ctx = Context()
    yield ctx

@pytest.mark.parametrize(
    "temp_queue",
    (True, False)
)
def test_return_result(ctx, temp_queue):
    async def main():
        connection = await ctx.get_rmq_con()
        await remote.init_channel_pool(connection)

        cons = remote.RemoteConsumer(connection, "one_lane", concurrency=1, limit_per_minute=0)
        await cons.consume()

        result = await remote.remote_call(
            remote_queue="one_lane",
            remote_func=operation.divide,
            remote_kwargs={"x": 6, "y": 3},
            return_result=True,
            result_temp_queue=temp_queue,
        )

        assert result == 2.0

        await cons.cancel()

    ctx.run(main()).result()


def test_remote_exception(ctx):
    async def main():
        connection = await ctx.get_rmq_con()
        await remote.init_channel_pool(connection)

        cons = remote.RemoteConsumer(connection, "one_lane", concurrency=1, limit_per_minute=0)
        await cons.consume()

        with pytest.raises(ZeroDivisionError):
            await remote.remote_call(
                remote_queue="one_lane",
                remote_func=operation.divide,
                remote_kwargs={"x": 6, "y": 0},
                return_result=True,
            )

    ctx.run(main()).result()

def test_timeout(ctx):
    async def main():
        connection = await ctx.get_rmq_con()
        await remote.init_channel_pool(connection)

        cons = remote.RemoteConsumer(connection, "one_lane", concurrency=1, limit_per_minute=0)
        await cons.consume()

        with pytest.raises(asyncio.TimeoutError):
            await remote.remote_call(
                remote_queue="one_lane",
                remote_func=operation.divide,
                remote_kwargs={"x": 6, "y": 3},
                return_result=True,
                expiration=0.01,  # Set a very short timeout
            )

    ctx.run(main()).result()


def test_no_result(ctx):
    async def main():
        connection = await ctx.get_rmq_con()
        await remote.init_channel_pool(connection)

        cons = remote.RemoteConsumer(connection, "one_lane", concurrency=1, limit_per_minute=0)
        await cons.consume()

        result = await remote.remote_call(
            remote_queue="one_lane",
            remote_func=operation.divide,
            remote_kwargs={"x": 6, "y": 3},
            return_result=False,
        )

        assert result is None

    ctx.run(main()).result()

