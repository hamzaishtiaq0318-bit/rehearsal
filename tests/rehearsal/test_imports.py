"""Every module imports cleanly, so a syntax slip anywhere fails the suite."""
import importlib
import pkgutil

import pytest

import rehearsal

MODULES = [m.name for m in pkgutil.iter_modules(rehearsal.__path__, "rehearsal.")]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)
