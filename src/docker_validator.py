"""Docker-based validator for running SWE-bench tests in isolated containers."""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# Python version mapping for SWE-bench repositories
# Based on the version field in the dataset
REPO_PYTHON_VERSIONS = {
    "astropy/astropy": "3.9",
    "django/django": "3.9",
    "matplotlib/matplotlib": "3.9",
    "mwaskom/seaborn": "3.9",
    "pallets/flask": "3.9",
    "psf/requests": "3.9",
    "pydata/xarray": "3.9",
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
        tests: list[str],
        timeout: int = 600,
        timeout_per_test: int = 120,
    ) -> DockerTestResult:
        """
        Run validation in a Docker container.

        Args:
            instance_id: Task identifier
            repo: Repository (e.g., "django/django")
            base_commit: Commit hash to checkout
            patch: Git diff patch to apply
            tests: List of test identifiers to run
            timeout: Overall timeout for the container
            timeout_per_test: Timeout per individual test

        Returns:
            DockerTestResult
        """
        # Determine Python version for this repo
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
            "patch": patch,
            "tests": tests,
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
    ) -> DockerTestResult:
        """
        Validate a patch against a SWE-bench task.

        Args:
            task: SWEBenchTask from the dataset
            patch: Git diff patch to apply (from Purple Agent)
            timeout: Overall timeout

        Returns:
            DockerTestResult
        """
        return self.run_validation(
            instance_id=task.instance_id,
            repo=task.repo,
            base_commit=task.base_commit,
            patch=patch,
            tests=task.fail_to_pass,
            timeout=timeout,
        )
