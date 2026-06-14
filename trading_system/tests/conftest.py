from __future__ import annotations

import asyncio
import inspect

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: run an async test function in an event loop")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    if "asyncio" not in pyfuncitem.keywords:
        return None
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None
    fixture_names = pyfuncitem._fixtureinfo.argnames
    test_args = {name: pyfuncitem.funcargs[name] for name in fixture_names}
    asyncio.run(test_function(**test_args))
    return True
