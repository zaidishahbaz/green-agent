"""Docker-based validator for running SWE-bench tests in isolated containers."""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# Python version mapping for SWE-bench repositories
# Based on official SWE-bench harness constants:
# https://github.com/swe-bench/SWE-bench/blob/main/swebench/harness/constants/python.py

def get_python_version(repo: str, version: str) -> str:
    """
    Get the appropriate Python version for a repo/version combination.
    Based on official SWE-bench harness specifications.
    """
    # Parse version to float for comparison (e.g., "4.2" -> 4.2)
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

    # Default Python 3.9 for these repos (all versions)
    elif repo in (
        "pytest-dev/pytest",
        "sympy/sympy",
        "sphinx-doc/sphinx",
        "psf/requests",
        "mwaskom/seaborn",
        "pylint-dev/pylint",
    ):
        return "3.9"

    # Fallback
    return "3.9"


# Legacy mapping for backward compatibility (uses Python 3.9 for all)
REPO_PYTHON_VERSIONS = {
    "astropy/astropy": "3.9",
    "django/django": "3.9",
    "matplotlib/matplotlib": "3.9",
    "mwaskom/seaborn": "3.9",
    "pallets/flask": "3.9",
    "psf/requests": "3.9",
    "pydata/xarray": "3.10",
    "pylint-dev/pylint": "3.9",
    "pytest-dev/pytest": "3.9",
    "scikit-learn/scikit-learn": "3.9",
    "sphinx-doc/sphinx": "3.9",
    "sympy/sympy": "3.9",
}

# Default image name
DEFAULT_IMAGE = "swebench-runner"


@dataclass
class DockerTestResult:
    """Result from running tests in Docker."""
    instance_id: str
    patch_applied: bool = False
    install_success: bool = False
    test_results: dict = field(default_factory=dict)
    fail_to_pass_results: dict = field(default_factory=dict)  # Results for fail_to_pass tests
    pass_to_pass_results: dict = field(default_factory=dict)  # Results for pass_to_pass tests
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
    """Runs SWE-bench validation in Docker containers."""

    def __init__(self, image_name: str = DEFAULT_IMAGE):
        self.image_name = image_name
        self._image_built = False

    def build_image(self, python_version: str = "3.9") -> tuple[bool, str]:
        """Build the SWE-bench runner Docker image."""
        dockerfile_path = Path(__file__).parent.parent / "docker" / "Dockerfile.swebench"

        if not dockerfile_path.exists():
            return False, f"Dockerfile not found: {dockerfile_path}"

        cmd = [
            "docker", "build",
            "-f", str(dockerfile_path),
            "--build-arg", f"PYTHON_VERSION={python_version}",
            "-t", f"{self.image_name}:{python_version}",
            str(dockerfile_path.parent.parent),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                return False, f"Build failed: {result.stderr}"
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "Build timed out"
        except Exception as e:
            return False, str(e)

    def image_exists(self, python_version: str = "3.9") -> bool:
        """Check if the Docker image exists."""
        cmd = ["docker", "image", "inspect", f"{self.image_name}:{python_version}"]
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0

    def ensure_image(self, python_version: str = "3.9") -> tuple[bool, str]:
        """Ensure the Docker image exists, building if necessary."""
        if self.image_exists(python_version):
            return True, ""
        return self.build_image(python_version)

    def run_validation(
        self,
        instance_id: str,
        repo: str,
        base_commit: str,
        patch: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str] | None = None,
        test_patch: str | None = None,
        timeout: int = 600,
        timeout_per_test: int = 120,
        environment_setup_commit: str | None = None,
        version: str | None = None,
    ) -> DockerTestResult:
        """
        Run validation in a Docker container.

        Args:
            instance_id: Task identifier
            repo: Repository (e.g., "django/django")
            base_commit: Commit hash to checkout for evaluation
            patch: Git diff patch to apply
            fail_to_pass: List of tests that should pass after the fix
            pass_to_pass: List of tests that should continue passing (regression check)
            test_patch: Patch to apply test files before running tests
            timeout: Overall timeout for the container
            timeout_per_test: Timeout per individual test
            environment_setup_commit: Commit hash for installing dependencies (optional)
            version: Package version string for Python version selection (optional)

        Returns:
            DockerTestResult
        """
        # Determine Python version for this repo/version combination
        if version:
            python_version = get_python_version(repo, version)
        else:
            python_version = REPO_PYTHON_VERSIONS.get(repo, "3.9")

        # Ensure image exists
        success, error = self.ensure_image(python_version)
        if not success:
            return DockerTestResult(
                instance_id=instance_id,
                errors=[f"Image build failed: {error}"]
            )

        # Prepare task input
        task_input = {
            "instance_id": instance_id,
            "repo": repo,
            "base_commit": base_commit,
            "environment_setup_commit": environment_setup_commit or base_commit,
            "patch": patch,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass or [],
            "test_patch": test_patch,
            "timeout_per_test": timeout_per_test,
        }

        # Run container
        # Note: Network access is needed for git clone
        cmd = [
            "docker", "run",
            "--rm",
            "-i",
            "--memory", "4g",
            "--cpus", "2",
            f"{self.image_name}:{python_version}",
        ]

        try:
            result = subprocess.run(
                cmd,
                input=json.dumps(task_input),
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            # Parse output
            try:
                output = json.loads(result.stdout)
                return DockerTestResult(
                    instance_id=output.get("instance_id", instance_id),
                    patch_applied=output.get("patch_applied", False),
                    install_success=output.get("install_success", False),
                    test_results=output.get("test_results", {}),
                    fail_to_pass_results=output.get("fail_to_pass_results", {}),
                    pass_to_pass_results=output.get("pass_to_pass_results", {}),
                    errors=output.get("errors", []),
                    summary=output.get("summary", {}),
                )
            except json.JSONDecodeError:
                return DockerTestResult(
                    instance_id=instance_id,
                    errors=[f"Invalid container output: {result.stdout[:500]}"]
                )

        except subprocess.TimeoutExpired:
            return DockerTestResult(
                instance_id=instance_id,
                errors=[f"Container timed out after {timeout}s"]
            )
        except Exception as e:
            return DockerTestResult(
                instance_id=instance_id,
                errors=[str(e)]
            )

    def validate_task(
        self,
        task,  # SWEBenchTask
        patch: str,
        timeout: int = 600,
        include_pass_to_pass: bool = True,
    ) -> DockerTestResult:
        """
        Validate a patch against a SWE-bench task.

        Args:
            task: SWEBenchTask from the dataset
            patch: Git diff patch to apply (from Purple Agent)
            timeout: Overall timeout
            include_pass_to_pass: Whether to also run pass_to_pass tests (regression check)

        Returns:
            DockerTestResult
        """
        pass_to_pass_tests = task.pass_to_pass if include_pass_to_pass else None

        if include_pass_to_pass and task.pass_to_pass:
            print(f"[Validator] Running {len(task.fail_to_pass)} fail_to_pass + {len(task.pass_to_pass)} pass_to_pass tests")
        else:
            print(f"[Validator] Running {len(task.fail_to_pass)} fail_to_pass tests")

        return self.run_validation(
            instance_id=task.instance_id,
            repo=task.repo,
            base_commit=task.base_commit,
            patch=patch,
            fail_to_pass=list(task.fail_to_pass),
            pass_to_pass=list(pass_to_pass_tests) if pass_to_pass_tests else None,
            test_patch=task.test_patch if hasattr(task, 'test_patch') else None,
            timeout=timeout,
            environment_setup_commit=task.environment_setup_commit,
            version=task.version,
        )
