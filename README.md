# Remote Producer and Consumer

This package provides asynchronous RPC (Remote Procedure Call) functionality over RabbitMQ using `aio_pika`. It includes
tools for both producing and consuming remote tasks, supporting concurrency, rate limiting, and callback patterns.

## `producer.py` (Remote Producer)

- **Channel Pooling**: Efficiently manages a pool of RabbitMQ channels for concurrent message publishing.
- **remote_call**: Sends an RPC request to a remote queue, optionally waiting for a result with timeout and priority
  support.
- **remote_callback**: Sends an RPC request and specifies a callback function to handle the result asynchronously.
- **remote_task**: Decorator to mark a function as a remote task, enabling it to be called via RabbitMQ.
- **remote_task_callback**: Decorator for remote tasks that require a callback upon completion.

## `consumer.py` (Remote Consumer)

- **RemoteConsumer**: Base class for consuming and processing messages from a RabbitMQ queue.
    - Handles message deserialization, function lookup, and invocation.
    - Supports concurrency and rate limiting (messages per minute).
    - Processes both request and result messages, including error handling and callback invocation.

---

These abstractions enable robust, asynchronous RPC workflows between distributed Python services using RabbitMQ.

## Usage

```python
import asyncio

import aio_pika
import remote
import random


@remote.remote_task(
    queue="rpc_lane",
    return_result=True
)
async def divide(x, y):
    print(f"DIV-thinking about {x} / {y}")
    await asyncio.sleep(random.random())
    print(f"DIV-returning {x} / {y}")
    return x / y


async def main() -> None:
    connection = await aio_pika.connect_robust()

    # Set up the consumer
    cons = remote.RemoteConsumer(connection, queue="rpc_lane", concurrency=1, limit_per_minute=0)
    await cons.consume()

    # Set up the producer
    await remote.init_channel_pool(connection)

    # Call the remote task - this interpreter is both producer and consumer
    print(await divide(6, 3))  # Should return 2.0

    await cons.cancel()
    await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
```