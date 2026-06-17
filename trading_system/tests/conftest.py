from __future__ import annotations

import asyncio
import inspect

from unittest.mock import patch

import pytest


class _DummyAlphaModel:
    def predict(self, _matrix):
        return [0.75]


_ALPHA_MODEL_PATCH = patch(
    "trading_system.app.alpha.ml_inference.load_alpha_model",
    return_value=_DummyAlphaModel(),
)
_ALPHA_MODEL_PATCH.start()


@pytest.fixture(autouse=True)
def mock_alpha_model_loader():
    """Keep tests independent from unversioned alpha model artifacts."""
    yield


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
