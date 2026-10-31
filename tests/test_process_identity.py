"""Observe actual OS process death rather than equating an expired lease with death."""

import subprocess
import sys

from refinvest.infrastructure.process_identity import process_identity, terminate_process


def test_actual_process_birth_and_death():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        birth = process_identity(child.pid)
        assert birth is not None
        assert process_identity(child.pid) == birth
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert process_identity(child.pid) is None


def test_termination_checks_birth_identity_before_signalling():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        birth = process_identity(child.pid)
        assert birth is not None
        terminate_process(child.pid, "wrong-process-birth")
        assert child.poll() is None
        terminate_process(child.pid, birth)
        child.wait(timeout=10)
        assert process_identity(child.pid) is None
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
