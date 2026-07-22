import os
import sys
from pathlib import Path

import adbc_drivers_validation.model
import adbc_drivers_validation.tests.conftest
import pytest
from adbc_drivers_validation.tests.conftest import (  # noqa: F401
    conn,
    conn_factory,
    db_kwargs,
    manual_test,
    pytest_collection_modifyitems,
)

from .chdb import ChdbQuirks


def pytest_addoption(parser):
    adbc_drivers_validation.tests.conftest.pytest_addoption(parser)
    parser.addoption("--vendor-version", action="store", default="26.5")


@pytest.fixture(scope="session")
def driver(request) -> adbc_drivers_validation.model.DriverQuirks:
    return ChdbQuirks()


def _import_module_first(path: str) -> None:
    """The Python module doubles as the driver, but its pybind runtime must
    be initialized by a normal import before the ADBC entrypoint is used."""
    if not path.endswith(".abi3.so"):
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("_chdb", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)


@pytest.fixture(scope="session")
def driver_path(driver) -> str:
    if os.environ.get("CHDB_LIB_PATH"):
        path = os.environ["CHDB_LIB_PATH"]
        _import_module_first(path)
        return path
    root = Path(__file__).parent.parent.parent.parent
    name = "libchdb.dylib" if sys.platform == "darwin" else "libchdb.so"
    for cand in (root / "libchdb.so", root / name, root / "buildlib" / name):
        if cand.exists():
            return str(cand)
    # Installed chdb package (wheel CI): the module doubles as the driver.
    import importlib.util

    spec = importlib.util.find_spec("chdb")
    if spec and spec.origin:
        cand = Path(spec.origin).parent / "_chdb.abi3.so"
        if cand.exists():
            _import_module_first(str(cand))
            return str(cand)
    pytest.skip("libchdb not found; set CHDB_LIB_PATH")


@pytest.fixture(scope="session", autouse=True)
def _provision_secondary_schema(driver, driver_path):
    """chDB is embedded: hold one connection for the whole session so the
    in-memory instance (and the secondary schema) survives across modules."""
    import adbc_driver_manager.dbapi

    conn = adbc_driver_manager.dbapi.connect(driver=driver_path, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {driver.features.secondary_schema}"
        )
    yield
    conn.close()
