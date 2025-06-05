############################################################################                                         #
#                                                                          #
# Licensed under the Apache License, Version 2.0 (the "License");          #
# you may not use this file except in compliance with the License.         #
# You may obtain a copy of the License at                                  #
#                                                                          #
#    https://www.apache.org/licenses/LICENSE-2.0                           #
#                                                                          #
# Unless required by applicable law or agreed to in writing, software      #
# distributed under the License is distributed on an "AS IS" BASIS,        #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
# See the License for the specific language governing permissions and      #
# limitations under the License.                                           #
############################################################################

import aio_pika
import pamqp

import contextlib
import functools
import asyncio
import pickle

from typing import Callable, Dict, Any, Awaitable, Union, AsyncGenerator
import time
import datetime as dt
from uuid import uuid4

from remote import util

import logging as py_logging

logging = py_logging.getLogger(__name__)

DateType = Union[int, float, dt.datetime, dt.timedelta]


class _ChannelPool:
    _instance: "_ChannelPool" = None

    @classmethod
    async def initialize(cls, connection: aio_pika.abc.AbstractRobustConnection, pool_size: int = 8):
        cls._instance = _ChannelPool(connection, pool_size)
        await cls._instance.start()

    @classmethod
    def instance(cls):
        if cls._instance:
            return cls._instance
        else:
            raise RuntimeError("ChannelPool not initialized. Call 'initialize()' first.")

    def __init__(self, connection, pool_size: int = 8):
        self.connection = connection
        self.pool_size = pool_size
        self._pool = asyncio.Queue(maxsize=pool_size)
        self._initialized = False

    async def start(self):
        # Create and put channels into the pool
        for _ in range(self.pool_size):
            channel: aio_pika.abc.AbstractChannel = await self.connection.channel()
            await channel.set_qos(prefetch_count=1)
            await self._pool.put(channel)
        self._initialized = True

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[aio_pika.channel.AbstractChannel, Any]:
        if not self._initialized:
            raise RuntimeError("ChannelPool not initialized. Call 'start()' first.")

        # Acquire a channel from the pool
        channel = await self._pool.get()

        try:
            yield channel  # Provide the channel for use within the context
        finally:
            # Release the channel back into the pool
            if channel.is_closed:
                # If the channel is closed, create a new one and put it back into the pool

                new_channel = await self.connection.channel()
                await new_channel.set_qos(prefetch_count=1)
                await self._pool.put(new_channel)
            else:
                # Otherwise, put the channel back into the pool
                await self._pool.put(channel)


def _fqn(func: Callable) -> str:
    return f"{func.__module__}.{func.__name__}"  # noqa


async def init_channel_pool(connection: aio_pika.abc.AbstractRobustConnection, pool_size: int = 8):
    """
    Initialize the channel pool with a given RabbitMQ connection and pool size.
    This function creates a pool of channels that can be reused for initiating all RPC calls.

    :param connection: The RabbitMQ connection to use.
    :param pool_size: The number of channels to maintain in the pool.
    """
    await _ChannelPool.initialize(connection, pool_size)
    logging.info(f"Channel pool initialized with {pool_size} channels.")


async def remote_call(
        remote_queue: str,
        remote_func: Callable[..., Awaitable[Any]] | str,
        remote_args: list | tuple = None,
        remote_kwargs: Dict[str, Any] = None,
        return_result: bool = False,
        result_temp_queue: bool = False,
        priority: int = 1,
        persistent: bool = False,
        expiration: DateType | None = None) -> Any | None:
    """

    Send an RPC request to a RabbitMQ queue.
    This function serializes the remote function and its arguments, sends it to the specified queue,
    and optionally waits for a result.

    :param remote_queue: The RabbitMQ queue to send the RPC request to.
    :param remote_func: The function to call remotely, can be a callable or a string (FQN).
    :param remote_args: Positional arguments to pass to the remote function.
    :param remote_kwargs: Keyword arguments to pass to the remote function.
    :param return_result: If True, the function will wait for a result.
    :param result_temp_queue: If True, a temporary queue will be created for the result.
    :param priority: The priority of the message, between 1 and 5.
    :param persistent: If True, the message will be persistent.
    :param expiration: The expiration time for the message, and timeout for the function call. can be a datetime, timedelta, or a timestamp.

    :return: the result of the remote function call if `return_result` is True, otherwise None.

    """

    if remote_args and not any([isinstance(remote_args, list), isinstance(remote_args, tuple)]):
        raise ValueError("remote_args must be a list or a tuple")

    if remote_kwargs and not isinstance(remote_kwargs, dict):
        raise ValueError("remote_kwargs must be a dict")

    if priority not in (1, 2, 3, 4, 5):
        raise ValueError("priority must be between 1 and 5")

    if expiration is not None:
        if isinstance(expiration, dt.datetime):
            return_timeout = expiration.timestamp() - time.time()
        elif isinstance(expiration, dt.timedelta):
            return_timeout = expiration.total_seconds()
        elif isinstance(expiration, (int, float)):
            if not expiration > 0:
                raise ValueError("expiration must be a positive number or a datetime/timedelta")
            return_timeout = expiration
        else:
            raise ValueError("expiration must be a datetime, timedelta, or a positive number")
    else:
        return_timeout = 0

    if isinstance(remote_func, str):
        remote_func = remote_func
    elif callable(remote_func):
        remote_func = _fqn(remote_func)
    else:
        raise ValueError("remote_func must be a callable or a string")

    headers = {
        "remote_func": remote_func,
    }
    payload = pickle.dumps((list(remote_args or []), remote_kwargs or {}))

    async with _ChannelPool.instance().acquire() as channel:
        msg = aio_pika.Message(
            type="REQUEST",
            body=payload,
            headers=headers,
            delivery_mode=aio_pika.abc.DeliveryMode.PERSISTENT if persistent
            else aio_pika.abc.DeliveryMode.NOT_PERSISTENT,
            timestamp=time.time(),
            priority=priority,
            expiration=expiration,
        )

        if return_result:
            _future = asyncio.get_running_loop().create_future()
            correlation_id = str(uuid4())
            msg.correlation_id = correlation_id
            if result_temp_queue:
                result_queue = await channel.declare_queue(
                    name=None,
                    exclusive=True,
                    auto_delete=True,
                )
                msg.reply_to = result_queue.name

            else:
                result_queue = await channel.get_queue("amq.rabbitmq.reply-to")
                msg.reply_to = "amq.rabbitmq.reply-to"

            cons_tag = str(uuid4())

            async def _on_reply(msg_reply: aio_pika.message.AbstractIncomingMessage):
                try:
                    if msg_reply.correlation_id != correlation_id:
                        logging.warning(
                            f"Cor-ID Mismatch: {msg_reply.correlation_id} != {correlation_id}")
                        logging.warning(msg_reply)
                        return

                    if msg_reply.type != "RESULT" or "error" not in msg_reply.headers:
                        logging.warning(f"Unknown or Missing Message Type: {msg_reply.type}")
                        logging.warning(msg_reply)
                        return

                    result = pickle.loads(msg_reply.body)
                    if msg_reply.headers["error"]:
                        _future.set_exception(result)
                    else:
                        _future.set_result(result)

                finally:
                    await result_queue.cancel(cons_tag)

            await result_queue.consume(_on_reply,
                                       no_ack=True,
                                       consumer_tag=cons_tag)

            _pub_stat = await channel.default_exchange.publish(
                msg,
                routing_key=remote_queue,
            )

            if isinstance(_pub_stat, pamqp.commands.Basic.Ack):
                async def _timeout():
                    await asyncio.sleep(return_timeout)
                    if not _future.done():
                        await result_queue.cancel(cons_tag)
                        _future.set_exception(asyncio.TimeoutError("remote Call Return Timeout"))

                if return_timeout > 0:
                    asyncio.create_task(_timeout())

                return await _future
            else:
                await result_queue.cancel(cons_tag)
                raise RuntimeError(f"remote Call was not successful: {_pub_stat}")

        else:
            _pub_stat = await channel.default_exchange.publish(
                msg,
                routing_key=remote_queue,
            )
            if isinstance(_pub_stat, pamqp.commands.Basic.Ack):
                return None
            else:
                raise RuntimeError(f"remote Call was not successful: {_pub_stat}")


async def remote_callback(
        remote_queue: str,
        remote_func: Callable[..., Awaitable[Any]] | str,
        callback_queue: str,
        callback_func: Callable[[asyncio.Future, str], Awaitable[Any]] | str,
        remote_args: list | tuple = None,
        remote_kwargs: Dict[str, Any] = None,
        correlation_id: str = None,
        priority: int = 1,
        persistent: bool = True,
        expiration: DateType | None = None) -> str:
    """
    Send a Remote Method Invocation (RPC) request to a RabbitMQ queue with a callback function.

    :param remote_queue: The name of the RabbitMQ queue to send the RPC request to.
    :param remote_func: The RPC function to call, can be a callable or a string (FQN).
    :param callback_queue: The RabbitMQ queue to send the callback result to.
    :param callback_func: The callback function to call with the result, can be a callable or a string (FQN).
    :param remote_args: A list or tuple of arguments to pass to the RPC function.
    :param remote_kwargs: A dictionary of keyword arguments to pass to the RPC function.
    :param correlation_id: Optional correlation ID for the message, if not provided a new one will be generated.
    :param priority: The priority of the message, must be between 1 and 5.
    :param persistent: Whether the message should be persistent (saved to disk).
    :param expiration: Optional expiration time for the message, can be a datetime, timedelta, or a float.

    :return: The correlation ID of the sent message.

    """
    if remote_args and not any([isinstance(remote_args, list), isinstance(remote_args, tuple)]):
        raise ValueError("remote_args must be a list or a tuple")

    if remote_kwargs and not isinstance(remote_kwargs, dict):
        raise ValueError("remote_kwargs must be a dict")

    if priority not in (1, 2, 3, 4, 5):
        raise ValueError("priority must be between 1 and 5")

    if isinstance(remote_func, str):
        remote_func = remote_func
    elif callable(remote_func):
        remote_func = _fqn(remote_func)
    else:
        raise ValueError("remote_func must be a callable or a string")

    if isinstance(callback_func, str):
        callback_func = callback_func
    elif callable(callback_func):
        util.verify_remote_callback_signature(callback_func)
        callback_func = _fqn(callback_func)
    else:
        raise ValueError("callback_func must be a callable or a string")

    headers = {
        "remote_func": remote_func,
        "callback_func": callback_func,
    }

    payload = pickle.dumps((list(remote_args or []), remote_kwargs or {}))

    if not correlation_id:
        correlation_id = str(uuid4())

    async with _ChannelPool.instance().acquire() as channel:

        msg = aio_pika.Message(
            type="REQUEST",
            reply_to=callback_queue,
            body=payload,
            headers=headers,
            delivery_mode=aio_pika.abc.DeliveryMode.PERSISTENT if persistent
            else aio_pika.abc.DeliveryMode.NOT_PERSISTENT,
            timestamp=time.time(),
            priority=priority,
            expiration=expiration,
            correlation_id=correlation_id,
        )

        _pub_stat = await channel.default_exchange.publish(
            msg,
            routing_key=remote_queue,
        )

        if isinstance(_pub_stat, pamqp.commands.Basic.Ack):
            return correlation_id
        else:
            raise RuntimeError(f"remote Call was not successful: {_pub_stat}")


def remote_task(queue: str,
                priority: int = 1,
                return_result: bool = False,
                expiration: DateType | None = None):
    """

    Decorator to mark a function as an RPC task. This will allow the function to be called
    remotely via RabbitMQ. The function will be serialized and sent to the specified queue.

    :param expiration: The expiration time for the message, and timeout for the call. can be a datetime, timedelta, or a timestamp.
    :param queue: The RabbitMQ queue to send the RPC request to.
    :param priority: The priority of the message, must be between 1 and 5.
    :param return_result: If True, the function will wait for a result.

    :return: A decorator that wraps the function to be called remotely.

    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            response = await remote_call(
                remote_queue=queue,
                remote_func=func,
                remote_args=args,
                remote_kwargs=kwargs,
                return_result=return_result,
                priority=priority,
                expiration=expiration
            )
            return response

        # Add markers for dynamic detection
        wrapper._is_remote_task = True
        wrapper._target_func = func
        return wrapper

    return decorator


def remote_task_callback(queue: str,
                         callback_queue: str,
                         callback_func: Callable[[asyncio.Future, str], Awaitable[Any]] | str,
                         priority: int = 1,
                         expiration: DateType | None = None):
    """
    Decorator to mark a function as an RPC task with a callback. This will allow the function to be called
    remotely via RabbitMQ, and a callback function will be invoked with the result.
    The function will be serialized and sent to the specified queue.

    :param expiration: The expiration time for the message, and timeout for the call. can be a datetime, timedelta, or a timestamp.
    :param queue: The RabbitMQ queue to send the RPC request to.
    :param callback_queue: The RabbitMQ queue to send the callback result to.
    :param callback_func: The callback function to call with the result, can be a callable or a string (FQN).
    :param priority: The priority of the message, must be between 1 and 5.

    :return: A decorator that wraps the function to be called remotely with a callback.

    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Task producer logic
            response = await remote_callback(
                remote_queue=queue,
                remote_func=func,
                callback_queue=callback_queue,
                callback_func=callback_func,
                remote_args=args,
                remote_kwargs=kwargs,
                priority=priority,
                expiration=expiration
            )
            return response

        # Add markers for dynamic detection
        wrapper._is_remote_task = True
        wrapper._target_func = func  # Reference to the original function
        return wrapper

    return decorator
