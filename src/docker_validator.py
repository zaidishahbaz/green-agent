"""Docker-based validator for running SWE-bench tests in existing containers."""

import subprocess
import re
from dataclasses import dataclass, field
from typing import Optional

# Container configuration
REPO_ROOT = "/workspace/repo"
DEFAULT_BASH_TIMEOUT = 120


def get_python_version(repo: str, version: str) -> str:
    """
    Get the appropriate Python version for a repo/version combination.
    Based on official SWE-bench harness specifications.
    """
    try:
        ver = float(version)
    except (ValueError, TypeError):
        ver = 0.0

    if repo == "django/django":
        if ver < 3.0:
            return "3.5"
        elif ver < 4.0:
            return "3.6"
        elif ver < 4.1:
            return "3.8"
        elif ver < 5.0:
            return "3.9"
        else:
            return "3.11"

    elif repo == "astropy/astropy":
        if ver < 3.0:
            return "3.6"
        elif ver < 5.3:
            return "3.9"
        else:
            return "3.10"

    elif repo == "matplotlib/matplotlib":
        if ver < 3.0:
            return "3.5"
        elif ver < 3.1:
            return "3.7"
        elif ver < 3.5:
            return "3.8"
        else:
            return "3.11"

    elif repo == "scikit-learn/scikit-learn":
        if ver < 1.0:
            return "3.6"
        else:
            return "3.9"

    elif repo == "pallets/flask":
        if ver < 2.1:
            return "3.9"
        elif ver < 2.2:
            return "3.10"
        else:
            return "3.11"

    elif repo == "pydata/xarray":
        return "3.10"

    elif repo in (
        "pytest-dev/pytest",
        "sympy/sympy",
        "sphinx-doc/sphinx",
        "psf/requests",
        "mwaskom/seaborn",
        "pylint-dev/pylint",
    ):
        return "3.9"

    return "3.9"


def get_debian_version(python_version: str) -> str:
    """
    Get the appropriate Debian version for a Python version.
    """
    try:
        ver = float(python_version)
    except (ValueError, TypeError):
        ver = 3.9

    if ver <= 3.7:
        return "buster"
    elif ver <= 3.9:
        return "bullseye"
    else:
        return "bookworm"


def _convert_unittest_to_django(test_name: str) -> str:
    """Convert unittest-style test name to Django runtests.py format."""
    match = re.match(r'(\w+)\s+\(([^)]+)\)', test_name)
    if match:
        method, path = match.groups()
        return f"{path}.{method}"
    return test_name


def _convert_unittest_to_pytest(test_name: str) -> str:
    """Convert unittest-style test name to pytest format."""
    match = re.match(r'(\w+)\s+\(([^)]+)\)', test_name)
    if match:
        method, path = match.groups()
        parts = path.rsplit('.', 1)
        if len(parts) == 2:
            module, classname = parts
            filepath = module.replace('.', '/') + '.py'
            return f"{filepath}::{classname}::{method}"
    return test_name


def _is_simple_test_name(test_name: str) -> bool:
    """Check if test name is just a function name (no path info)."""
    return bool(re.match(r'^test_\w+$', test_name))


def get_individual_test_command(repo: str, version: str, test_name: str) -> str:
    """
    Get the command to run a specific individual test for a repo.
    Based on official SWE-bench harness specifications.
    """
    try:
        ver = float(version) if version else 0.0
    except (ValueError, TypeError):
        ver = 0.0

    if repo == "django/django":
        django_test = _convert_unittest_to_django(test_name)
        if ver == 1.9:
            return f"python tests/runtests.py {django_test} -v 2"
        else:
            return f"python tests/runtests.py --settings=test_sqlite --parallel 1 {django_test} -v 2"

    elif repo == "sympy/sympy":
        return f"PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose {test_name}"

    elif repo == "sphinx-doc/sphinx":
        pytest_test = _convert_unittest_to_pytest(test_name)
        return f"tox --current-env -epy39 -v -- {pytest_test}"

    elif repo == "astropy/astropy":
        pytest_test = _convert_unittest_to_pytest(test_name)
        return f"python -m pytest -rA -vv -o console_output_style=classic --tb=short {pytest_test}"

    elif repo in ("matplotlib/matplotlib", "scikit-learn/scikit-learn", "pallets/flask",
                  "pydata/xarray", "pytest-dev/pytest", "psf/requests", "pylint-dev/pylint"):
        pytest_test = _convert_unittest_to_pytest(test_name)
        return f"python -m pytest -rA -xvs --tb=short {pytest_test}"

    elif repo == "mwaskom/seaborn":
        pytest_test = _convert_unittest_to_pytest(test_name)
        return f"python -m pytest --no-header -rA -xvs --tb=short {pytest_test}"

    # Default: use pytest with smart test name handling
    if _is_simple_test_name(test_name):
        return f"python -m pytest -k {test_name} -xvs --tb=short"
    else:
        pytest_test = _convert_unittest_to_pytest(test_name)
        return f"python -m pytest {pytest_test} -xvs --tb=short"


@dataclass
class DockerTestResult:
    """Result from running tests in Docker."""

    instance_id: str
    patch_applied: bool = False
    install_success: bool = False
    test_results: dict = field(default_factory=dict)
    fail_to_pass_results: dict = field(default_factory=dict)
    pass_to_pass_results: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def score(self) -> float:
        return self.summary.get("score", 0.0)

    @property
    def tests_passed(self) -> int:
        return self.summary.get("passed", 0)

    @property
    def tests_failed(self) -> int:
        return self.summary.get("failed", 0)

    @property
    def fail_to_pass_score(self) -> float:
        """Score for fail_to_pass tests (should all pass after fix)."""
        return self.summary.get("fail_to_pass_score", 0.0)

    @property
    def pass_to_pass_score(self) -> float:
        """Score for pass_to_pass tests (regression check)."""
        return self.summary.get("pass_to_pass_score", 0.0)


class DockerValidator:
    """Runs SWE-bench validation using exec in existing container."""

    def __init__(self, container_id: Optional[str] = None):
        self.container_id = container_id

    def _exec_in_container(
        self,
        command: str,
        cwd: str = REPO_ROOT,
        timeout: int = DEFAULT_BASH_TIMEOUT,
    ) -> tuple[bool, str, str]:
        """
        Execute a command inside the container.

        Returns:
            (success, stdout, stderr)
        """
        if not self.container_id:
            return False, "", "Container not set"

        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-w",
                    cwd,
                    self.container_id,
                    "bash",
                    "-c",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return (
                result.returncode == 0,
                result.stdout[:10000] if result.stdout else "",
                result.stderr[:2000] if result.stderr else "",
            )
        except subprocess.TimeoutExpired:
            return False, "", f"Command timed out after {timeout}s"
        except Exception as e:
            return False, "", str(e)

    def _run_single_test(
        self,
        test_name: str,
        repo: str,
        version: str,
        timeout: int = DEFAULT_BASH_TIMEOUT,
    ) -> dict:
        """Run a single test and return result."""
        cmd = get_individual_test_command(repo, version, test_name)

        print(f"[Validator] Running test: {test_name}")
        print(f"[Validator] Command: {cmd}")

        success, stdout, stderr = self._exec_in_container(cmd, timeout=timeout)

        status = "PASSED" if success else "FAILED"
        print(f"[Validator] {test_name}: {status}")

        return {
            "name": test_name,
            "passed": success,
            "output": (stdout + stderr)[-2000:],
        }

    def run_validation(
        self,
        instance_id: str,
        repo: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str] | None = None,
        timeout_per_test: int = 120,
        version: str | None = None,
    ) -> DockerTestResult:
        """
        Run validation tests in the existing container.

        The patch should already be applied in the container.
        This method only runs tests - no cloning, installing, or patching.

        Args:
            instance_id: Task identifier
            repo: Repository (e.g., "django/django")
            fail_to_pass: List of tests that should pass after the fix
            pass_to_pass: List of tests that should continue passing
            timeout_per_test: Timeout per individual test
            version: Package version string for test command selection

        Returns:
            DockerTestResult
        """
        if not self.container_id:
            return DockerTestResult(
                instance_id=instance_id,
                errors=["No container_id set for validation"]
            )

        result = DockerTestResult(
            instance_id=instance_id,
            patch_applied=True,  # Patch was already applied
            install_success=True,  # Dependencies already installed
        )

        version = version or ""
        pass_to_pass = pass_to_pass or []

        print(f"[Validator] Running {len(fail_to_pass)} fail_to_pass tests")
        print(f"[Validator] Running {len(pass_to_pass)} pass_to_pass tests")

        # Run fail_to_pass tests
        for test in fail_to_pass:
            test_result = self._run_single_test(test, repo, version, timeout_per_test)
            result.fail_to_pass_results[test] = test_result
            result.test_results[test] = test_result

        # Run pass_to_pass tests
        for test in pass_to_pass:
            test_result = self._run_single_test(test, repo, version, timeout_per_test)
            result.pass_to_pass_results[test] = test_result
            result.test_results[test] = test_result

        # Calculate summary
        f2p_passed = sum(1 for r in result.fail_to_pass_results.values() if r["passed"])
        f2p_total = len(result.fail_to_pass_results)
        p2p_passed = sum(1 for r in result.pass_to_pass_results.values() if r["passed"])
        p2p_total = len(result.pass_to_pass_results)

        passed = f2p_passed + p2p_passed
        total = f2p_total + p2p_total

        result.summary = {
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "score": passed / total if total > 0 else 0.0,
            "fail_to_pass_passed": f2p_passed,
            "fail_to_pass_total": f2p_total,
            "fail_to_pass_score": f2p_passed / f2p_total if f2p_total > 0 else 0.0,
            "pass_to_pass_passed": p2p_passed,
            "pass_to_pass_total": p2p_total,
            "pass_to_pass_score": p2p_passed / p2p_total if p2p_total > 0 else 0.0,
        }

        print(f"[Validator] Summary: {result.summary}")

        return result

    def validate_task(
        self,
        task,  # SWEBenchTask
        timeout_per_test: int = 120,
        include_pass_to_pass: bool = True,
    ) -> DockerTestResult:
        """
        Validate a patch against a SWE-bench task.

        Args:
            task: SWEBenchTask from the dataset
            timeout_per_test: Timeout per individual test
            include_pass_to_pass: Whether to also run pass_to_pass tests

        Returns:
            DockerTestResult
        """
        pass_to_pass_tests = list(task.pass_to_pass) if include_pass_to_pass and task.pass_to_pass else None

        return self.run_validation(
            instance_id=task.instance_id,
            repo=task.repo,
            fail_to_pass=list(task.fail_to_pass),
            pass_to_pass=pass_to_pass_tests,
            timeout_per_test=timeout_per_test,
            version=task.version,
        )
