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

import asyncio
import pickle

from typing import Callable, Union
import time
import datetime as dt

import logging as py_logging

from remote.util import callable_by_fqn, verify_remote_callback_signature

logging = py_logging.getLogger(__name__)

DateType = Union[int, float, dt.datetime, dt.timedelta]


class RemoteConsumer:
    """
    Base class for RabbitMQ RPC consumers.
    This class handles consuming messages from a RabbitMQ queue and processing them
    using remote functions defined in the application.
    It supports concurrency and rate limiting to control the flow of message processing.

    The consumer listens for messages on a specified queue and processes them
    by invoking the remote function specified in the message headers.


    """

    def __init__(self,
                 connection: aio_pika.connection.Connection | aio_pika.abc.AbstractRobustConnection,
                 queue: str | None = None,
                 exclusive: bool = False,
                 auto_delete: bool = True,
                 durable: bool = False,
                 concurrency: int = 1,
                 limit_per_minute: int = 60):
        """
        Create an RPCConsumer instance to consume messages from a RabbitMQ queue.

        :param connection: The RabbitMQ connection to use.
        :param queue: The name of the RabbitMQ queue to consume from.
        :param exclusive: If True, the queue will be exclusive to this consumer.
        :param auto_delete: If True, the queue will be deleted when the consumer is cancelled.
        :param durable: If True, the queue will be durable (survives broker restarts).
        :param concurrency: The number of messages to process concurrently.
        :param limit_per_minute: The maximum number of messages to process per minute.

        """

        self.connection = connection
        self.queue_name = queue
        self.concurrency = concurrency
        self.limit_per_minute = limit_per_minute

        self.exclusive = exclusive
        self.auto_delete = auto_delete
        self.durable = durable

        self.consume_channel = None
        self.result_channel = None
        self.queue: aio_pika.Queue | None = None
        self.consumer_tag = None

        self.current_count = 0
        self.current_minute = time.time() // 60

    async def consume(self) -> str:
        """
        Create a queue and start consuming messages

        :return: The name of the queue being consumed

        """
        self.consume_channel = await self.connection.channel()
        self.result_channel = await self.connection.channel()

        await self.consume_channel.set_qos(prefetch_count=self.concurrency)
        args = {"x-max-priority": 5}
        self.queue = await self.consume_channel.declare_queue(self.queue_name,
                                                              exclusive=self.exclusive,
                                                              auto_delete=self.auto_delete,
                                                              durable=self.durable,
                                                              arguments=args)
        self.consumer_tag = await self.queue.consume(self._on_message,
                                                     no_ack=False)

        return self.queue.name

    async def queue_name(self) -> str | None:
        if self.queue:
            return self.queue.name
        else:
            return None

    async def cancel(self):
        await self.queue.cancel(self.consumer_tag)
        await self.consume_channel.close()
        await self.result_channel.close()

    def _reset_counter(self):
        instant_minute = time.time() // 60
        if instant_minute != self.current_minute:  # Reset the counter at the top of the minute
            self.current_count = 0
            self.current_minute = instant_minute

    async def _throttle(self):
        if self.limit_per_minute > 0:
            self._reset_counter()
            self.current_count += 1
            if self.current_count > self.limit_per_minute:
                await asyncio.sleep(60 - time.time() % 60)  # Sleep until the top of the minute
                self._reset_counter()

    async def _on_message(self, message: aio_pika.message.AbstractIncomingMessage):
        async with message.process(ignore_processed=True):
            try:
                remote_func = message.headers.get("remote_func")
                if not remote_func:
                    logging.warning(f"remote_func not found in message headers: {message}")
                    return

                if message.type not in "REQUEST, RESULT":
                    logging.warning(f"Unknown message type: {message}")
                    return

                try:
                    func = callable_by_fqn(remote_func)
                    func = _get_target_function(func)


                except Exception as ex:
                    logging.warning(f"remote function not found: {remote_func}")
                    logging.exception(ex)
                    return

                await self._throttle()

                if message.type == "REQUEST":
                    await self._handle_request(message, func)
                elif message.type == "RESULT":
                    await self._handle_callback_result(message, func)
                else:
                    logging.warning(f"Unknown message type: {message.type}")

            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logging.exception(ex)
                if message.redelivered:
                    await message.reject(requeue=False)
                else:
                    await message.nack(requeue=True)

    async def _handle_request(self, message, func):
        try:
            args, kwargs = pickle.loads(message.body)
            result = await func(*args, **kwargs)

            result_bytes = pickle.dumps(result)
            error = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            result_bytes = pickle.dumps(e)
            error = True

        if message.reply_to:
            result_headers = {
                "error": error,
            }

            if message.headers.get("callback_func"):
                result_headers["remote_func"] = message.headers.get("callback_func")

            result_msg = aio_pika.Message(
                type="RESULT",
                body=result_bytes,
                headers=result_headers,
                timestamp=time.time(),
                correlation_id=message.correlation_id,
                delivery_mode=message.delivery_mode,
                priority=message.priority,
            )

            _pub_stat = await self.result_channel.default_exchange.publish(
                result_msg,
                routing_key=message.reply_to,
            )

        else:
            pass

    async def _handle_callback_result(self, message, func):  # noqa
        result = pickle.loads(message.body)

        _fut: asyncio.Future = asyncio.get_running_loop().create_future()

        if message.headers.get("error"):
            _fut.set_exception(result)
        else:
            _fut.set_result(result)
        try:
            verify_remote_callback_signature(func)
            await func(_fut, message.correlation_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception(e)


def _is_remote_task(func) -> bool:
    return getattr(func, "_is_remote_task", False)


def _get_target_function(func) -> Callable | None:
    if _is_remote_task(func):
        return getattr(func, "_target_func", None)
    else:
        return func
