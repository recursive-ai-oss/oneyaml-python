import os
import subprocess
import sys


def test_forced_native_without_module_raises_import_error():
    # Hide the extension by making its import fail, as on a platform without a wheel.
    code = "import sys; sys.modules['oneyaml._native'] = None\nimport oneyaml\n"
    env = {**os.environ, "ONEYAML_BACKEND": "native"}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "ImportError" in r.stderr and "ONEYAML_BACKEND=native" in r.stderr

    env["ONEYAML_BACKEND"] = "auto"
    code += "print(oneyaml.backend)"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "pure"
