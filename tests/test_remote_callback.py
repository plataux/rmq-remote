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


def test_result_callback(ctx):
    async def main():
        connection = await ctx.get_rmq_con()
        await remote.init_channel_pool(connection)

        cons = remote.RemoteConsumer(connection, "rpc_lane", concurrency=1, limit_per_minute=0)
        await cons.consume()

        await remote.remote_callback(
            remote_queue="rpc_lane",
            remote_func=operation.divide,
            callback_queue="rpc_lane",
            callback_func=operation.remote_callback,
            remote_kwargs={"x": 6, "y": 3},
        )
        result = await operation.result_queue.get()
        assert await result == 2.0

    ctx.run(main()).result()


def test_exception_callback(ctx):
    async def main():
        connection = await ctx.get_rmq_con()
        await remote.init_channel_pool(connection)

        cons = remote.RemoteConsumer(connection, "rpc_lane", concurrency=1, limit_per_minute=0)
        await cons.consume()

        await remote.remote_callback(
            remote_queue="rpc_lane",
            remote_func=operation.divide,
            callback_queue="rpc_lane",
            callback_func=operation.remote_callback,
            remote_kwargs={"x": 6, "y": 0},
        )

        with pytest.raises(ZeroDivisionError):
            result: asyncio.Future = await operation.result_queue.get()
            await result

    ctx.run(main()).result()


@pytest.mark.parametrize(
    "callback_func",
    (operation.divide_result_bad_callback_1,
     operation.divide_result_bad_callback_2)
)
def test_bad_callback(ctx, callback_func):
    async def main():
        connection = await ctx.get_rmq_con()
        await remote.init_channel_pool(connection)

        cons = remote.RemoteConsumer(connection, "rpc_lane", concurrency=1, limit_per_minute=0)
        await cons.consume()

        with pytest.raises(TypeError):
            await remote.remote_callback(
                remote_queue="rpc_lane",
                remote_func=operation.divide,
                callback_queue="rpc_lane",
                callback_func=callback_func,  # noqa
                remote_kwargs={"x": 6, "y": 3},
            )

    ctx.run(main()).result()
