"""
Shared test utility functions for SWE-bench test execution.

This module provides common test name conversion and command generation
functions used by both container_executor.py and docker_validator.py.
"""

import re


def convert_unittest_to_django(test_name: str) -> str:
    """Convert unittest-style test name to Django runtests.py format.

    Input:  "test_method (module.ClassName)"
    Output: "module.ClassName.test_method"
    """
    match = re.match(r'(\w+)\s+\(([^)]+)\)', test_name)
    if match:
        method, path = match.groups()
        return f"{path}.{method}"
    return test_name


def convert_unittest_to_pytest(test_name: str) -> str:
    """Convert unittest-style test name to pytest format.

    Input:  "test_method (module.ClassName)"
    Output: "module.py::ClassName::test_method"
    """
    match = re.match(r'(\w+)\s+\(([^)]+)\)', test_name)
    if match:
        method, path = match.groups()
        parts = path.rsplit('.', 1)
        if len(parts) == 2:
            module, classname = parts
            # Convert module path to file path
            filepath = module.replace('.', '/') + '.py'
            return f"{filepath}::{classname}::{method}"
    return test_name


def is_simple_test_name(test_name: str) -> bool:
    """Check if test name is just a function name (no path info)."""
    # Simple test names: test_foo, test_bar_baz
    return bool(re.match(r'^test_\w+$', test_name))


def get_individual_test_command(
    repo: str, version: str, test_name: str, python_bin: str = "python"
) -> str:
    """
    Get the command to run a specific individual test for a repo.

    Args:
        repo: Repository name (e.g., "django/django")
        version: Version string (e.g., "3.0")
        test_name: Test identifier from SWE-bench
        python_bin: Python binary path (default: "python")

    Returns:
        Full command string to run the test
    """
    try:
        ver = float(version) if version else 0.0
    except (ValueError, TypeError):
        ver = 0.0

    if repo == "django/django":
        # Django uses tests/runtests.py with a specific format
        django_test = convert_unittest_to_django(test_name)
        if ver == 1.9:
            return f"{python_bin} tests/runtests.py {django_test} -v 2"
        else:
            return f"{python_bin} tests/runtests.py --settings=test_sqlite --parallel 1 {django_test} -v 2"

    elif repo == "sympy/sympy":
        # SymPy uses bin/test with specific flags
        # Test names are usually like "sympy/core/tests/test_basic.py"
        return f"PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose {test_name}"

    elif repo == "sphinx-doc/sphinx":
        # Sphinx uses tox
        pytest_test = convert_unittest_to_pytest(test_name)
        return f"tox --current-env -epy39 -v -- {pytest_test}"

    elif repo == "astropy/astropy":
        pytest_test = convert_unittest_to_pytest(test_name)
        return f"{python_bin} -m pytest -rA -vv -o console_output_style=classic --tb=short {pytest_test}"

    elif repo in (
        "matplotlib/matplotlib",
        "scikit-learn/scikit-learn",
        "pallets/flask",
        "pydata/xarray",
        "pytest-dev/pytest",
        "psf/requests",
        "pylint-dev/pylint",
    ):
        pytest_test = convert_unittest_to_pytest(test_name)
        return f"{python_bin} -m pytest -rA -xvs --tb=short {pytest_test}"

    elif repo == "mwaskom/seaborn":
        pytest_test = convert_unittest_to_pytest(test_name)
        return f"{python_bin} -m pytest --no-header -rA -xvs --tb=short {pytest_test}"

    # Default: use pytest with smart test name handling
    if is_simple_test_name(test_name):
        # Simple test name like "test_foo" - use -k for keyword match
        return f"{python_bin} -m pytest -k {test_name} -xvs --tb=short"
    else:
        # Try to convert to pytest format
        pytest_test = convert_unittest_to_pytest(test_name)
        return f"{python_bin} -m pytest {pytest_test} -xvs --tb=short"
