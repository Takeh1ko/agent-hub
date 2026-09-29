from hub.gate.acceptance import acceptance_cmd, acceptance_paths

CARD = """# T — x

**Цель.** ц

**Приёмка.** `pytest -q tests/test_a.py tests/test_b.py::T::t`; ещё
`venv/bin/python -m pytest -q -k "not slow" tests/test_c.py` и повтор `pytest tests/test_a.py`.

**Нельзя.** `pytest tests/не_приёмка.py`
"""


def test_paths_from_acceptance_only():
    assert acceptance_paths(CARD) == ["tests/test_a.py", "tests/test_b.py::T::t", "tests/test_c.py"]


def test_cmd_and_fallback():
    assert acceptance_cmd(CARD, "py")[:4] == ["py", "-m", "pytest", "-q"]
    assert acceptance_cmd("# T\n\n**Приёмка.** глазами\n", "py") == ["py", "-m", "pytest", "-q"]
