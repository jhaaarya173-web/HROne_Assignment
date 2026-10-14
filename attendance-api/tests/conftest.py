"""Test fixtures. The tests talk to a real uvicorn process (so concurrency is real) and a real MongoDB.

    pip install -r requirements.txt pytest httpx
    pytest -q                       # MONGO_URI defaults to mongodb://localhost:27017

Two throwaway databases are used: attendance_test (rebuilt for the API tests) and attendance_big
(the ~100k-record dataset from tests/dataset.py, loaded once and reused).
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient

from tests import dataset

ROOT = Path(__file__).resolve().parent.parent
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(db_name: str):
    port = _free_port()
    env = {**os.environ, "MONGO_URI": MONGO_URI, "MONGO_DB": db_name}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20  # the contract's readiness budget
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health").status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("API did not become ready within 20 s")
    return proc, base


@pytest.fixture(scope="session")
def mongo():
    return MongoClient(MONGO_URI, tz_aware=True)


@pytest.fixture(scope="module")
def api(mongo):
    mongo.drop_database("attendance_test")
    proc, base = _serve("attendance_test")
    with httpx.Client(base_url=base, timeout=30) as client:
        yield client
    proc.terminate()
    proc.wait()


@pytest.fixture(scope="session")
def big(mongo):
    """The large dataset: (http client, employees, logs). Reloaded only when it is missing."""
    db = mongo["attendance_big"]
    employees, logs = dataset.build()
    if db.attendance_logs.estimated_document_count() != len(logs):
        dataset.load(db)
    proc, base = _serve("attendance_big")
    with httpx.Client(base_url=base, timeout=60) as client:
        yield client, employees, logs
    proc.terminate()
    proc.wait()
