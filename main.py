import os
import sys
import importlib.util

# Resolve absolute path to the backend directory and file
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "OrderFlow-AI", "backend"))
backend_main = os.path.join(backend_dir, "main.py")

if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# Load the backend application without circular import conflicts
spec = importlib.util.spec_from_file_location("orderflow_backend_main", backend_main)
backend_module = importlib.util.module_from_spec(spec)
sys.modules["orderflow_backend_main"] = backend_module
spec.loader.exec_module(backend_module)

app = backend_module.app
