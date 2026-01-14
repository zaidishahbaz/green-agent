"""
Docker container executor for running bash commands in isolated environments.

This module manages persistent Docker containers for SWE-bench task execution:
- Clones repo at base_commit
- Enforces OS-level read/exec permissions (no writes except via patch)
- Tracks current working directory
- Prevents navigation outside repo root
- Returns structured output {cwd, stdout, stderr}
"""

import json
import subprocess
import uuid
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from swebench import SWEBenchTask


# Container configuration
DEFAULT_IMAGE = "swebench-bash"
REPO_ROOT = "/workspace/repo"
DEFAULT_BASH_TIMEOUT = 30
DEFAULT_CONTAINER_MEMORY = "4g"
DEFAULT_CONTAINER_CPUS = "2"


@dataclass
class BashResult:
    """Result from executing a bash command in the container."""
    cwd: str
    stdout: str
    stderr: str
    success: bool = True
    error: Optional[str] = None


@dataclass
class PatchResult:
    """Result from applying a patch in the container."""
    success: bool
    cwd: str
    stdout: str
    stderr: str
    error: Optional[str] = None


class ContainerExecutor:
    """
    Manages a persistent Docker container for bash execution.

    The container:
    - Has the repo cloned at base_commit
    - Has read/exec permissions only (no writes)
    - Tracks current working directory
    - Enforces repo boundary (no cd outside repo root)
    """

    def __init__(self, image_name: str = DEFAULT_IMAGE):
        self.image_name = image_name
        self.container_id: Optional[str] = None
        self.cwd = REPO_ROOT
        self.repo_root = REPO_ROOT
        self.task: Optional[SWEBenchTask] = None
        self.python_version = "3.9"
        self._started = False

    @property
    def is_running(self) -> bool:
        """Check if container is currently running."""
        if not self.container_id:
            return False
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.container_id],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.stdout.strip() == "true"
        except Exception:
            return False

    def _get_python_version(self, repo: str, version: str) -> str:
        """Get appropriate Python version for repo/version combination."""
        try:
            ver = float(version)
        except (ValueError, TypeError):
            ver = 0.0

        if repo == "django/django":
            if ver < 3.0:
                return "3.8"
            elif ver < 4.0:
                return "3.8"
            elif ver < 4.1:
                return "3.8"
            elif ver < 5.0:
                return "3.9"
            else:
                return "3.11"
        elif repo == "astropy/astropy":
            if ver < 3.0:
                return "3.9"
            elif ver < 5.3:
                return "3.9"
            else:
                return "3.10"
        elif repo == "matplotlib/matplotlib":
            if ver < 3.5:
                return "3.8"
            else:
                return "3.11"
        elif repo == "scikit-learn/scikit-learn":
            return "3.9"
        elif repo == "pallets/flask":
            return "3.10"
        elif repo == "pydata/xarray":
            return "3.10"
        else:
            return "3.9"

    def _ensure_image(self) -> tuple[bool, str]:
        """Ensure the Docker image exists, building if necessary."""
        # Check if image exists
        result = subprocess.run(
            ["docker", "image", "inspect", f"{self.image_name}:{self.python_version}"],
            capture_output=True
        )
        if result.returncode == 0:
            return True, ""

        # Build image
        dockerfile_path = Path(__file__).parent.parent / "docker" / "Dockerfile.bash"
        if not dockerfile_path.exists():
            return False, f"Dockerfile not found: {dockerfile_path}"

        try:
            result = subprocess.run(
                [
                    "docker", "build",
                    "-f", str(dockerfile_path),
                    "--build-arg", f"PYTHON_VERSION={self.python_version}",
                    "-t", f"{self.image_name}:{self.python_version}",
                    str(dockerfile_path.parent.parent),
                ],
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

    async def start(self, task: SWEBenchTask) -> tuple[bool, str]:
        """
        Start a container for the given task.

        This will:
        1. Build/ensure Docker image exists
        2. Start a persistent container
        3. Clone the repo at environment_setup_commit
        4. Install dependencies
        5. Checkout to base_commit
        6. Set read/exec permissions (remove write)

        Args:
            task: The SWEBenchTask to work on

        Returns:
            (success, error_message)
        """
        self.task = task
        self.cwd = REPO_ROOT

        # Get Python version for this repo
        self.python_version = self._get_python_version(task.repo, task.version)

        # Ensure image exists
        success, error = self._ensure_image()
        if not success:
            return False, f"Image build failed: {error}"

        # Generate unique container name
        container_name = f"swebench-{task.instance_id.replace('/', '-')}-{uuid.uuid4().hex[:8]}"

        # Start container in detached mode with tail -f to keep it alive
        try:
            result = subprocess.run(
                [
                    "docker", "run",
                    "-d",  # Detached mode
                    "--name", container_name,
                    "--memory", DEFAULT_CONTAINER_MEMORY,
                    "--cpus", DEFAULT_CONTAINER_CPUS,
                    "-w", REPO_ROOT,
                    f"{self.image_name}:{self.python_version}",
                    "tail", "-f", "/dev/null"  # Keep container alive
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False, f"Failed to start container: {result.stderr}"

            self.container_id = result.stdout.strip()
        except Exception as e:
            return False, f"Container start error: {e}"

        # Clone repo at environment_setup_commit
        clone_result = self._exec_in_container(
            f"git clone --quiet https://github.com/{task.repo}.git {REPO_ROOT}",
            cwd="/workspace",
            timeout=300
        )
        if not clone_result.success:
            await self.stop()
            return False, f"Clone failed: {clone_result.stderr}"

        # Checkout environment_setup_commit for dependency installation
        checkout_result = self._exec_in_container(
            f"git checkout --quiet {task.environment_setup_commit}",
            timeout=60
        )
        if not checkout_result.success:
            await self.stop()
            return False, f"Checkout environment_setup_commit failed: {checkout_result.stderr}"

        # Install dependencies
        install_result = self._install_dependencies()
        if not install_result.success:
            # Continue anyway - some tests might work
            print(f"Warning: Dependency installation failed: {install_result.stderr}")

        # Checkout to base_commit for evaluation
        if task.environment_setup_commit != task.base_commit:
            checkout_result = self._exec_in_container(
                f"git checkout --quiet {task.base_commit}",
                timeout=60
            )
            if not checkout_result.success:
                await self.stop()
                return False, f"Checkout base_commit failed: {checkout_result.stderr}"

        # Set read/exec permissions (remove write permissions)
        # This enforces OS-level security instead of whitelist
        perm_result = self._exec_in_container(
            f"chmod -R a-w {REPO_ROOT} && chmod -R a+rX {REPO_ROOT}",
            timeout=60
        )
        if not perm_result.success:
            print(f"Warning: Permission setting failed: {perm_result.stderr}")

        self._started = True
        return True, ""

    def _install_dependencies(self) -> BashResult:
        """Install dependencies in the container."""
        # Try different installation methods
        install_commands = [
            "pip install -e . -q 2>/dev/null",
            "pip install -e .[dev] -q 2>/dev/null",
            "pip install -e .[test] -q 2>/dev/null",
        ]

        for cmd in install_commands:
            result = self._exec_in_container(cmd, timeout=600)
            if result.success:
                return result

        # Try requirements files
        req_files = ["requirements.txt", "requirements-dev.txt", "test-requirements.txt"]
        for req_file in req_files:
            check_result = self._exec_in_container(f"test -f {req_file}", timeout=10)
            if check_result.success:
                result = self._exec_in_container(
                    f"pip install -r {req_file} -q",
                    timeout=600
                )
                if result.success:
                    return result

        return BashResult(
            cwd=self.cwd,
            stdout="",
            stderr="Could not install dependencies",
            success=False
        )

    def _exec_in_container(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: int = DEFAULT_BASH_TIMEOUT
    ) -> BashResult:
        """
        Execute a command inside the container.

        Args:
            command: Shell command to execute
            cwd: Working directory (defaults to current cwd)
            timeout: Command timeout in seconds

        Returns:
            BashResult with cwd, stdout, stderr
        """
        if not self.container_id:
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr="Container not started",
                success=False,
                error="Container not started"
            )

        work_dir = cwd or self.cwd

        try:
            result = subprocess.run(
                [
                    "docker", "exec",
                    "-w", work_dir,
                    self.container_id,
                    "bash", "-c", command
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return BashResult(
                cwd=work_dir,
                stdout=result.stdout[:10000] if result.stdout else "",
                stderr=result.stderr[:2000] if result.stderr else "",
                success=result.returncode == 0
            )
        except subprocess.TimeoutExpired:
            return BashResult(
                cwd=work_dir,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                success=False,
                error=f"Timeout after {timeout}s"
            )
        except Exception as e:
            return BashResult(
                cwd=work_dir,
                stdout="",
                stderr=str(e),
                success=False,
                error=str(e)
            )

    def _resolve_path(self, target: str) -> str:
        """Resolve a path relative to current cwd."""
        if target.startswith("/"):
            # Absolute path
            return os.path.normpath(target)
        else:
            # Relative path
            return os.path.normpath(os.path.join(self.cwd, target))

    def _is_within_repo(self, path: str) -> bool:
        """Check if path is within repo root."""
        normalized = os.path.normpath(path)
        return normalized.startswith(self.repo_root)

    async def execute_bash(self, command: str, timeout: int = DEFAULT_BASH_TIMEOUT) -> BashResult:
        """
        Execute a bash command in the container with cwd tracking.

        Handles 'cd' commands specially to track working directory and
        enforce repo boundary.

        Args:
            command: Bash command to execute
            timeout: Command timeout in seconds

        Returns:
            BashResult with {cwd, stdout, stderr, success}
        """
        if not self._started or not self.container_id:
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr="Container not started",
                success=False,
                error="Container not started"
            )

        command = command.strip()

        # Handle cd command specially for cwd tracking
        if command == "cd" or command.startswith("cd "):
            return await self._handle_cd(command)

        # Handle compound commands with cd (e.g., "cd src && ls")
        if " && " in command or " ; " in command:
            return await self._handle_compound_command(command, timeout)

        # Execute regular command
        result = self._exec_in_container(command, timeout=timeout)
        result.cwd = self.cwd  # Ensure cwd is current
        return result

    async def _handle_cd(self, command: str) -> BashResult:
        """Handle cd command with boundary enforcement."""
        # Parse target directory
        parts = command.split(maxsplit=1)
        if len(parts) == 1:
            # Just "cd" - go to repo root
            target = self.repo_root
        else:
            target = parts[1].strip().strip("'\"")

        # Handle special cases
        if target == "-":
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr="cd - not supported",
                success=False
            )

        if target == "~" or target.startswith("~/"):
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr=f"Cannot cd outside repo root ({self.repo_root})",
                success=False
            )

        # Resolve the path
        new_cwd = self._resolve_path(target)

        # Check boundary
        if not self._is_within_repo(new_cwd):
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr=f"Cannot cd outside repo root ({self.repo_root})",
                success=False
            )

        # Verify directory exists in container
        check_result = self._exec_in_container(f"test -d '{new_cwd}'", timeout=10)
        if not check_result.success:
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr=f"bash: cd: {target}: No such file or directory",
                success=False
            )

        # Update cwd
        self.cwd = new_cwd
        return BashResult(
            cwd=self.cwd,
            stdout="",
            stderr="",
            success=True
        )

    async def _handle_compound_command(self, command: str, timeout: int) -> BashResult:
        """
        Handle compound commands (with && or ;) that may include cd.

        We need to track cd commands within compound commands to maintain
        correct cwd state.
        """
        # For compound commands, we execute them and then query the final pwd
        # First, execute the command
        result = self._exec_in_container(command, timeout=timeout)

        # If successful and command contained cd, update our cwd
        if result.success and ("cd " in command or command.startswith("cd")):
            # Query the actual pwd after command execution
            pwd_result = self._exec_in_container("pwd", timeout=10)
            if pwd_result.success:
                new_cwd = pwd_result.stdout.strip()
                if self._is_within_repo(new_cwd):
                    self.cwd = new_cwd

        result.cwd = self.cwd
        return result

    async def apply_patch(self, patch: str) -> PatchResult:
        """
        Apply a git patch to the repository.

        Temporarily enables write permissions, applies patch, then
        re-enables read-only mode.

        Args:
            patch: Git diff format patch

        Returns:
            PatchResult with success status and stderr for retry
        """
        if not self._started or not self.container_id:
            return PatchResult(
                success=False,
                cwd=self.cwd,
                stdout="",
                stderr="Container not started",
                error="Container not started"
            )

        if not patch or not patch.strip():
            return PatchResult(
                success=False,
                cwd=self.cwd,
                stdout="",
                stderr="Empty patch provided",
                error="Empty patch"
            )

        # Temporarily enable write permissions
        self._exec_in_container(f"chmod -R u+w {REPO_ROOT}", timeout=60)

        # Write patch to file using docker cp via stdin (more robust than shell escaping)
        try:
            # Use docker exec with stdin to write the patch file
            write_proc = subprocess.run(
                [
                    "docker", "exec", "-i",
                    self.container_id,
                    "tee", "/tmp/patch.diff"
                ],
                input=patch,
                capture_output=True,
                text=True,
                timeout=30
            )
            if write_proc.returncode != 0:
                self._exec_in_container(f"chmod -R a-w {REPO_ROOT}", timeout=60)
                return PatchResult(
                    success=False,
                    cwd=self.cwd,
                    stdout="",
                    stderr=f"Failed to write patch file: {write_proc.stderr}",
                    error="Failed to write patch"
                )
        except Exception as e:
            self._exec_in_container(f"chmod -R a-w {REPO_ROOT}", timeout=60)
            return PatchResult(
                success=False,
                cwd=self.cwd,
                stdout="",
                stderr=f"Failed to write patch file: {e}",
                error="Failed to write patch"
            )

        # Try to apply patch
        apply_result = self._exec_in_container(
            f"cd {REPO_ROOT} && git apply --verbose /tmp/patch.diff",
            timeout=60
        )

        if not apply_result.success:
            # Try with --3way
            apply_result = self._exec_in_container(
                f"cd {REPO_ROOT} && git apply --3way /tmp/patch.diff",
                timeout=60
            )

        # Clean up patch file
        self._exec_in_container("rm -f /tmp/patch.diff", timeout=10)

        # Restore read-only permissions
        self._exec_in_container(f"chmod -R a-w {REPO_ROOT} && chmod -R a+rX {REPO_ROOT}", timeout=60)

        return PatchResult(
            success=apply_result.success,
            cwd=self.cwd,
            stdout=apply_result.stdout,
            stderr=apply_result.stderr,
            error=None if apply_result.success else "Patch application failed"
        )

    async def stop(self):
        """Stop and remove the container."""
        if self.container_id:
            try:
                subprocess.run(
                    ["docker", "stop", self.container_id],
                    capture_output=True,
                    timeout=30
                )
                subprocess.run(
                    ["docker", "rm", "-f", self.container_id],
                    capture_output=True,
                    timeout=30
                )
            except Exception as e:
                print(f"Warning: Failed to stop container: {e}")
            finally:
                self.container_id = None
                self._started = False

    def get_status(self) -> dict:
        """Get current container status."""
        return {
            "container_id": self.container_id,
            "is_running": self.is_running,
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "task_id": self.task.instance_id if self.task else None,
        }
