import importlib

def test_imports():
    # Basic import test for all main modules
    for mod in ("app", "cli", "converter"):
        importlib.import_module(mod)
