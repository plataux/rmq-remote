import importlib
import inspect
import asyncio
from typing import Callable, Any

import logging as py_logging

logging = py_logging.getLogger(__name__)


def verify_remote_callback_signature(func: Callable):
    try:
        signature = inspect.signature(func)
    except TypeError as e:
        raise TypeError(f"Could not get signature for '{func.__name__}': {e}")

    if not inspect.iscoroutinefunction(func):
        raise TypeError(f"'{func.__name__}' is not an async function.")

    parameters = list(signature.parameters.values())

    # 2. Check the number of parameters
    if len(parameters) != 2:
        raise TypeError(f"{func.__name__} expects {len(parameters)} arguments, but 2 are required.")

    # Check the first parameter
    first_param = parameters[0]
    # Check if the parameter kind is suitable (positional or keyword)
    if first_param.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY):
        raise TypeError(
            f"First parameter of '{func.__name__}' must be a positional/keyword argument. Found Kind: {first_param.kind}")

    # Check if the type hint is compatible with asyncio.Future (or Any/missing)
    if first_param.annotation is not inspect.Parameter.empty and \
            first_param.annotation is not Any and \
            not (inspect.isclass(first_param.annotation) and issubclass(first_param.annotation, asyncio.Future)):
        logging.warning(f"First parameter of '{func.__name__}' has a type hint ({first_param.annotation}) "
                        f"that is not asyncio.Future or Any")

    # Check the second parameter
    second_param = parameters[1]
    # Check if the parameter kind is suitable (positional or keyword)
    if second_param.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY):
        raise TypeError(
            f"Second parameter of '{func.__name__}' must be a positional/keyword argument. Found Kind: {second_param.kind}")

    # Check if the type hint is compatible with str (or Any/missing)
    if second_param.annotation is not inspect.Parameter.empty and \
            second_param.annotation is not Any and \
            not (inspect.isclass(second_param.annotation) and issubclass(second_param.annotation, str)):
        logging.warning(
            f"Second parameter of '{func.__name__}' has a type hint ({second_param.annotation}), that is not str or Any")

    return True


def callable_by_fqn(remote_func) -> Callable:
    # Split the module path from the function path
    module_name, _, func_name = remote_func.rpartition('.')

    # Dynamically import the module
    module = importlib.import_module(module_name)

    # Traverse the function path to get to the final function object
    func_obj = getattr(module, func_name)

    if callable(func_obj):
        return func_obj
    else:
        raise RuntimeError(f"remote function is not callable: {func_obj}")
