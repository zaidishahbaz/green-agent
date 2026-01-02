"""Validation module for applying patches and running tests."""

import os
import subprocess
import tempfile
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestResult:
    """Result of running a single test."""
    name: str
    passed: bool
    output: str = ""
    error: str = ""


@dataclass
class ValidationResult:
    """Result of validating a patch against tests."""
    instance_id: str
    patch_applied: bool
    patch_error: str = ""
    fail_to_pass_results: dict[str, TestResult] = field(default_factory=dict)
    pass_to_pass_results: dict[str, TestResult] = field(default_factory=dict)

    @property
    def tests_fixed(self) -> int:
        """Number of fail_to_pass tests that now pass."""
        return sum(1 for r in self.fail_to_pass_results.values() if r.passed)

    @property
    def tests_broken(self) -> int:
        """Number of pass_to_pass tests that now fail."""
        return sum(1 for r in self.pass_to_pass_results.values() if not r.passed)

    @property
    def score(self) -> float:
        """Calculate score: fraction of fail_to_pass tests fixed."""
        if not self.fail_to_pass_results:
            return 0.0
        return self.tests_fixed / len(self.fail_to_pass_results)

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "patch_applied": self.patch_applied,
            "patch_error": self.patch_error,
            "tests_fixed": self.tests_fixed,
            "tests_broken": self.tests_broken,
            "total_fail_to_pass": len(self.fail_to_pass_results),
            "total_pass_to_pass": len(self.pass_to_pass_results),
            "score": self.score,
            "fail_to_pass_results": {
                k: {"passed": v.passed, "output": v.output[:500]}
                for k, v in self.fail_to_pass_results.items()
            },
        }


class RepoValidator:
    """Validates patches by cloning repos, applying patches, and running tests."""

    # GitHub URL template
    GITHUB_URL = "https://github.com/{repo}.git"

    def __init__(self, work_dir: str | None = None, keep_repos: bool = False):
        """
        Initialize the validator.

        Args:
            work_dir: Directory for cloning repos. If None, uses temp directory.
            keep_repos: If True, don't delete repos after validation.
        """
        self.work_dir = Path(work_dir) if work_dir else None
        self.keep_repos = keep_repos
        self._temp_dir = None

    def _get_work_dir(self) -> Path:
        """Get or create the working directory."""
        if self.work_dir:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            return self.work_dir

        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="swebench_")
        return Path(self._temp_dir)

    def _run_command(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        timeout: int = 300
    ) -> tuple[int, str, str]:
        """
        Run a command and return (return_code, stdout, stderr).
        """
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout}s"
        except Exception as e:
            return -1, "", str(e)

    def clone_repo(self, repo: str, base_commit: str, target_dir: Path) -> tuple[bool, str]:
        """
        Clone a repository at a specific commit.

        Args:
            repo: Repository in format "owner/name"
            base_commit: Commit hash to checkout
            target_dir: Directory to clone into

        Returns:
            (success, error_message)
        """
        url = self.GITHUB_URL.format(repo=repo)

        # Clone the repo
        code, stdout, stderr = self._run_command(
            ["git", "clone", "--quiet", url, str(target_dir)],
            timeout=120
        )
        if code != 0:
            return False, f"Clone failed: {stderr}"

        # Checkout the specific commit
        code, stdout, stderr = self._run_command(
            ["git", "checkout", "--quiet", base_commit],
            cwd=target_dir
        )
        if code != 0:
            return False, f"Checkout failed: {stderr}"

        return True, ""

    def apply_patch(self, patch: str, repo_dir: Path) -> tuple[bool, str]:
        """
        Apply a git diff patch to a repository.

        Args:
            patch: The patch content (git diff format)
            repo_dir: Path to the repository

        Returns:
            (success, error_message)
        """
        if not patch or not patch.strip():
            return False, "Empty patch"

        # Write patch to a temp file
        patch_file = repo_dir / "patch.diff"
        patch_file.write_text(patch)

        # Apply the patch
        code, stdout, stderr = self._run_command(
            ["git", "apply", "--verbose", "patch.diff"],
            cwd=repo_dir
        )

        # Clean up patch file
        patch_file.unlink(missing_ok=True)

        if code != 0:
            # Try with --3way for better merge handling
            patch_file.write_text(patch)
            code, stdout, stderr = self._run_command(
                ["git", "apply", "--3way", "patch.diff"],
                cwd=repo_dir
            )
            patch_file.unlink(missing_ok=True)

            if code != 0:
                return False, f"Patch failed: {stderr}"

        return True, ""

    def setup_venv(self, repo_dir: Path, timeout: int = 60) -> tuple[bool, str]:
        """
        Create a virtual environment in the repo directory.

        Returns:
            (success, error_message)
        """
        venv_dir = repo_dir / ".venv"
        if venv_dir.exists():
            return True, ""  # Already exists

        code, stdout, stderr = self._run_command(
            ["python3", "-m", "venv", ".venv"],
            cwd=repo_dir,
            timeout=timeout
        )
        if code != 0:
            return False, f"venv creation failed: {stderr}"
        return True, ""

    def _get_venv_python(self, repo_dir: Path) -> str:
        """Get the path to the venv python executable."""
        venv_python = repo_dir / ".venv" / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
        return "python3"

    def _get_venv_pip(self, repo_dir: Path) -> str:
        """Get the path to the venv pip executable."""
        venv_pip = repo_dir / ".venv" / "bin" / "pip"
        if venv_pip.exists():
            return str(venv_pip)
        return "pip"

    def install_repo(self, repo_dir: Path, timeout: int = 300) -> tuple[bool, str]:
        """
        Create venv and install repo dependencies.

        Args:
            repo_dir: Path to the repository
            timeout: Installation timeout

        Returns:
            (success, error_message)
        """
        # First create venv
        success, error = self.setup_venv(repo_dir)
        if not success:
            return False, error

        pip = self._get_venv_pip(repo_dir)

        # Upgrade pip first
        self._run_command([pip, "install", "--upgrade", "pip", "-q"], cwd=repo_dir, timeout=60)

        # Install pytest
        code, stdout, stderr = self._run_command(
            [pip, "install", "pytest", "-q"],
            cwd=repo_dir,
            timeout=120
        )

        # Try different installation methods
        install_commands = [
            [pip, "install", "-e", ".", "-q"],
            [pip, "install", "-e", ".[dev]", "-q"],
            [pip, "install", "-e", ".[test]", "-q"],
        ]

        # First try setup.py or pyproject.toml install
        for cmd in install_commands:
            code, stdout, stderr = self._run_command(cmd, cwd=repo_dir, timeout=timeout)
            if code == 0:
                return True, ""

        # Try requirements files
        req_files = ["requirements.txt", "requirements-dev.txt", "test-requirements.txt"]
        for req_file in req_files:
            if (repo_dir / req_file).exists():
                code, stdout, stderr = self._run_command(
                    [pip, "install", "-r", req_file, "-q"],
                    cwd=repo_dir,
                    timeout=timeout
                )
                if code == 0:
                    return True, ""

        return False, "Could not install dependencies"

    def run_test(
        self,
        test_name: str,
        repo_dir: Path,
        timeout: int = 60
    ) -> TestResult:
        """
        Run a single pytest test.

        Args:
            test_name: Test identifier (e.g., "tests/test_foo.py::test_bar")
            repo_dir: Path to the repository
            timeout: Test timeout in seconds

        Returns:
            TestResult
        """
        python = self._get_venv_python(repo_dir)

        code, stdout, stderr = self._run_command(
            [python, "-m", "pytest", test_name, "-xvs", "--tb=short"],
            cwd=repo_dir,
            timeout=timeout
        )

        passed = code == 0
        output = stdout + stderr

        return TestResult(
            name=test_name,
            passed=passed,
            output=output,
            error="" if passed else stderr
        )

    def run_tests(
        self,
        tests: list[str],
        repo_dir: Path,
        timeout_per_test: int = 60
    ) -> dict[str, TestResult]:
        """
        Run multiple tests.

        Args:
            tests: List of test identifiers
            repo_dir: Path to the repository
            timeout_per_test: Timeout per test in seconds

        Returns:
            Dict mapping test name to TestResult
        """
        results = {}
        for test in tests:
            results[test] = self.run_test(test, repo_dir, timeout_per_test)
        return results

    def validate(
        self,
        instance_id: str,
        repo: str,
        base_commit: str,
        patch: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str] | None = None,
        timeout_per_test: int = 60,
    ) -> ValidationResult:
        """
        Validate a patch by cloning repo, applying patch, and running tests.

        Args:
            instance_id: Task identifier
            repo: Repository in format "owner/name"
            base_commit: Commit to checkout
            patch: Git diff patch to apply
            fail_to_pass: Tests that should pass after patch
            pass_to_pass: Tests that should still pass (regression check)
            timeout_per_test: Timeout per test in seconds

        Returns:
            ValidationResult
        """
        result = ValidationResult(instance_id=instance_id, patch_applied=False)

        # Create a unique directory for this task
        work_dir = self._get_work_dir()
        repo_dir = work_dir / instance_id.replace("/", "_").replace("__", "_")

        # Clean up if exists
        if repo_dir.exists():
            shutil.rmtree(repo_dir)

        try:
            # Clone the repo
            success, error = self.clone_repo(repo, base_commit, repo_dir)
            if not success:
                result.patch_error = f"Clone failed: {error}"
                return result

            # Apply the patch
            success, error = self.apply_patch(patch, repo_dir)
            if not success:
                result.patch_error = f"Patch failed: {error}"
                return result

            result.patch_applied = True

            # Run fail_to_pass tests
            if fail_to_pass:
                result.fail_to_pass_results = self.run_tests(
                    fail_to_pass, repo_dir, timeout_per_test
                )

            # Run pass_to_pass tests (optional, for regression check)
            if pass_to_pass:
                result.pass_to_pass_results = self.run_tests(
                    pass_to_pass, repo_dir, timeout_per_test
                )

        finally:
            # Clean up
            if not self.keep_repos and repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)

        return result

    def cleanup(self):
        """Clean up temporary directories."""
        if self._temp_dir and os.path.exists(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
