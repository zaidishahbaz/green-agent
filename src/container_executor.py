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

import re

from swebench import SWEBenchTask
from docker_validator import get_python_version


# Container configuration
DEFAULT_IMAGE = "swebench-bash"
REPO_ROOT = "/workspace/repo"
AGENT_TEMP_DIR = f"{REPO_ROOT}/.agent_temp"
DEFAULT_BASH_TIMEOUT = 30

# Blocked paths - prevent access to sensitive system directories
BLOCKED_PATHS = (
    "/tmp",
    "/var/tmp",
    "/etc",
    "/root",
    "/home",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/log",
)
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
        self.protected_test_files: list[str] = []

    @staticmethod
    def _extract_files_from_patch(patch: str) -> list[str]:
        """Extract file paths from a unified diff patch."""
        files = []
        # Match lines like "+++ b/path/to/file.py" or "+++ path/to/file.py"
        for match in re.finditer(r'^\+\+\+ (?:b/)?(.+)$', patch, re.MULTILINE):
            filepath = match.group(1).strip()
            if filepath and filepath != '/dev/null':
                files.append(filepath)
        return files

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
        self.python_version = get_python_version(task.repo, task.version)

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

        # Checkout base_commit for evaluation
        checkout_result = self._exec_in_container(
            f"git checkout --quiet {task.base_commit}",
            timeout=60
        )
        if not checkout_result.success:
            await self.stop()
            return False, f"Checkout base_commit failed: {checkout_result.stderr}"

        # Extract requirements files from environment_setup_commit without switching branches
        # Use git show to get file contents from that commit
        self._exec_in_container("mkdir -p /tmp/env_reqs", timeout=10)
        for req_file in ["requirements.txt", "requirements-dev.txt", "test-requirements.txt",
                         "requirements_dev.txt", "environment.yml", "environment.yaml"]:
            self._exec_in_container(
                f"git show {task.environment_setup_commit}:{req_file} > /tmp/env_reqs/{req_file} 2>/dev/null || true",
                timeout=10
            )

        # Install external dependencies from saved requirements files
        install_result = self._install_external_dependencies()
        if not install_result.success:
            print(f"Warning: External dependency installation failed: {install_result.stderr}")

        # Install the package itself at base_commit
        pkg_install_result = self._install_package()
        if not pkg_install_result.success:
            print(f"Warning: Package installation failed: {pkg_install_result.stderr}")

        # Create .agent_temp directory for agent scratch space
        self._exec_in_container(f"mkdir -p {AGENT_TEMP_DIR}", timeout=10)
        # Add to .gitignore so it doesn't interfere with git operations
        self._exec_in_container(
            f"echo '.agent_temp/' >> {REPO_ROOT}/.gitignore",
            timeout=10
        )

        # Apply test_patch if present (tests needed for evaluation)
        # These files will be protected from modification by the agent
        if task.test_patch:
            self.protected_test_files = self._extract_files_from_patch(task.test_patch)
            if self.protected_test_files:
                # Write test patch to temp file
                try:
                    write_proc = subprocess.run(
                        [
                            "docker", "exec", "-i",
                            self.container_id,
                            "tee", f"{AGENT_TEMP_DIR}/test_patch.diff"
                        ],
                        input=task.test_patch,
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    if write_proc.returncode == 0:
                        # Apply the test patch
                        apply_result = self._exec_in_container(
                            f"cd {REPO_ROOT} && git apply --whitespace=fix --verbose {AGENT_TEMP_DIR}/test_patch.diff",
                            timeout=60
                        )
                        if not apply_result.success:
                            print(f"Warning: Test patch application failed: {apply_result.stderr}")
                        # Clean up
                        self._exec_in_container(f"rm -f {AGENT_TEMP_DIR}/test_patch.diff", timeout=10)
                except Exception as e:
                    print(f"Warning: Failed to apply test patch: {e}")

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

    def _install_external_dependencies(self) -> BashResult:
        """Install external dependencies from saved requirements files."""
        # Install from requirements files saved from environment_setup_commit
        req_files = ["requirements.txt", "requirements-dev.txt", "test-requirements.txt", "requirements_dev.txt"]
        installed = False

        for req_file in req_files:
            check_result = self._exec_in_container(f"test -f /tmp/env_reqs/{req_file}", timeout=10)
            if check_result.success:
                result = self._exec_in_container(
                    f"pip install -r /tmp/env_reqs/{req_file} -q",
                    timeout=600
                )
                if result.success:
                    installed = True
                    print(f"[Container] Installed dependencies from {req_file}")

        if installed:
            return BashResult(cwd=self.cwd, stdout="", stderr="", success=True)

        return BashResult(
            cwd=self.cwd,
            stdout="",
            stderr="No requirements files found",
            success=False
        )

    def _install_package(self) -> BashResult:
        """Install the package itself in editable mode at current commit."""
        # Try different installation methods for the package
        install_commands = [
            "pip install -e . -q 2>/dev/null",
            "pip install -e .[dev] -q 2>/dev/null",
            "pip install -e .[test] -q 2>/dev/null",
        ]

        for cmd in install_commands:
            result = self._exec_in_container(cmd, timeout=600)
            if result.success:
                print(f"[Container] Package installed with: {cmd}")
                return result

        return BashResult(
            cwd=self.cwd,
            stdout="",
            stderr="Could not install package",
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

    def _contains_blocked_path(self, command: str) -> str | None:
        """Check if command tries to access blocked system paths.

        Returns the blocked path if found, None otherwise.
        """
        for blocked in BLOCKED_PATHS:
            # Check for absolute paths to blocked directories
            if blocked in command:
                # Make sure it's actually a path reference, not just a substring
                # e.g., "pytest" shouldn't trigger on "/tmp" being in command
                patterns = [
                    f" {blocked}",      # space before (argument)
                    f" {blocked}/",     # path with subdir
                    f"'{blocked}",      # quoted path
                    f'"{blocked}',      # double-quoted path
                    f">{blocked}",      # redirect to
                    f"<{blocked}",      # redirect from
                    f"cat {blocked}",   # explicit cat
                    f"ls {blocked}",    # explicit ls
                ]
                if command.startswith(blocked) or any(p in command for p in patterns):
                    return blocked
        return None

    def _check_git_restriction(self, command: str) -> BashResult | None:
        """
        Check if a git command tries to access commits after base_commit.

        This prevents the agent from seeing the fix commit or future commits.
        Returns a BashResult with error if blocked, None if allowed.
        """
        if not self.task:
            return None

        # References that could reveal future commits
        BLOCKED_REFS = ["HEAD", "main", "master", "origin/main", "origin/master", "origin/HEAD"]

        # Commands that could reveal future commits
        git_cmd = command.strip()

        # git log restrictions
        if git_cmd.startswith("git log"):
            # Block if using HEAD, main, master without explicit base_commit restriction
            for ref in BLOCKED_REFS:
                if ref in git_cmd and self.task.base_commit not in git_cmd:
                    return BashResult(
                        cwd=self.cwd,
                        stdout="",
                        stderr=f"git log with '{ref}' is restricted. Use 'git log {self.task.base_commit}' or earlier commits.",
                        success=False,
                        error="Restricted git command"
                    )

        # git show restrictions
        if git_cmd.startswith("git show"):
            # Block HEAD, main, master references
            for ref in BLOCKED_REFS:
                if ref in git_cmd:
                    return BashResult(
                        cwd=self.cwd,
                        stdout="",
                        stderr=f"git show with '{ref}' is restricted. Use specific commit hashes at or before {self.task.base_commit[:8]}.",
                        success=False,
                        error="Restricted git command"
                    )
            # Block bare "git show" (shows HEAD by default)
            if git_cmd.strip() == "git show":
                return BashResult(
                    cwd=self.cwd,
                    stdout="",
                    stderr=f"git show without arguments is restricted. Use 'git show <commit-hash>' for commits at or before {self.task.base_commit[:8]}.",
                    success=False,
                    error="Restricted git command"
                )

        # git diff restrictions (could show changes between base and fix)
        if git_cmd.startswith("git diff"):
            for ref in BLOCKED_REFS:
                if ref in git_cmd:
                    return BashResult(
                        cwd=self.cwd,
                        stdout="",
                        stderr=f"git diff with '{ref}' is restricted. Use 'git diff' for unstaged changes or specific older commits.",
                        success=False,
                        error="Restricted git command"
                    )

        # git checkout restrictions (prevent checking out fix commit)
        if git_cmd.startswith("git checkout"):
            for ref in BLOCKED_REFS:
                if ref in git_cmd:
                    return BashResult(
                        cwd=self.cwd,
                        stdout="",
                        stderr=f"git checkout '{ref}' is restricted. The repo is checked out at base_commit {self.task.base_commit[:8]}.",
                        success=False,
                        error="Restricted git command"
                    )

        # git reset restrictions
        if git_cmd.startswith("git reset"):
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr="git reset is restricted.",
                success=False,
                error="Restricted git command"
            )

        # git pull/fetch restrictions
        if git_cmd.startswith("git pull") or git_cmd.startswith("git fetch"):
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr="git pull/fetch is restricted. The repo is in a fixed state.",
                success=False,
                error="Restricted git command"
            )

        return None

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

        # Check for blocked path access
        blocked = self._contains_blocked_path(command)
        if blocked:
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr=f"Access denied: {blocked} is outside the allowed workspace",
                success=False,
                error="Blocked path access"
            )

        # Restrict git commands to prevent looking at commits after base_commit
        # This prevents the agent from seeing the fix commit
        git_restricted = self._check_git_restriction(command)
        if git_restricted:
            return git_restricted

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

        # Check if patch tries to modify protected test files
        patch_files = self._extract_files_from_patch(patch)
        protected_violations = [f for f in patch_files if f in self.protected_test_files]
        if protected_violations:
            return PatchResult(
                success=False,
                cwd=self.cwd,
                stdout="",
                stderr=f"Cannot modify protected test files: {', '.join(protected_violations)}",
                error="Protected file modification attempted"
            )

        # Temporarily enable write permissions (excluding protected test files)
        self._exec_in_container(f"chmod -R u+w {REPO_ROOT}", timeout=60)

        # Re-apply read-only to protected test files
        if self.protected_test_files:
            for test_file in self.protected_test_files:
                self._exec_in_container(
                    f"chmod a-w {REPO_ROOT}/{test_file} 2>/dev/null || true",
                    timeout=10
                )

        # Write patch to file using docker cp via stdin (more robust than shell escaping)
        patch_file = f"{AGENT_TEMP_DIR}/patch.diff"
        try:
            # Use docker exec with stdin to write the patch file
            write_proc = subprocess.run(
                [
                    "docker", "exec", "-i",
                    self.container_id,
                    "tee", patch_file
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

        # Try to apply patch with multiple fallback strategies
        apply_result = self._exec_in_container(
            f"cd {REPO_ROOT} && git apply --whitespace=fix --verbose {patch_file}",
            timeout=60
        )

        if not apply_result.success:
            # Try with --3way for merge conflicts
            apply_result = self._exec_in_container(
                f"cd {REPO_ROOT} && git apply --whitespace=fix --3way {patch_file}",
                timeout=60
            )

        if not apply_result.success:
            # Fallback to patch command which is more lenient
            apply_result = self._exec_in_container(
                f"cd {REPO_ROOT} && patch -p1 --ignore-whitespace < {patch_file}",
                timeout=60
            )

        # Clean up patch file
        self._exec_in_container(f"rm -f {patch_file}", timeout=10)

        # Restore read-only permissions
        self._exec_in_container(f"chmod -R a-w {REPO_ROOT} && chmod -R a+rX {REPO_ROOT}", timeout=60)

        return PatchResult(
            success=apply_result.success,
            cwd=self.cwd,
            stdout=apply_result.stdout,
            stderr=apply_result.stderr,
            error=None if apply_result.success else "Patch application failed"
        )

    async def execute_debug(
        self,
        patch: str,
        command: str,
        timeout: int = DEFAULT_BASH_TIMEOUT
    ) -> BashResult:
        """
        Execute a debug session in an isolated container.

        This creates a temporary snapshot of the current state, applies a patch,
        runs a command with write access (except test files), returns results,
        and automatically rolls back (destroys the temp container).

        Use this to test patches before committing them.

        Args:
            patch: Git diff patch to apply
            command: Bash command to run after applying patch
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

        # Check if patch tries to modify protected test files
        if patch:
            patch_files = self._extract_files_from_patch(patch)
            protected_violations = [f for f in patch_files if f in self.protected_test_files]
            if protected_violations:
                return BashResult(
                    cwd=self.cwd,
                    stdout="",
                    stderr=f"Cannot modify protected test files: {', '.join(protected_violations)}",
                    success=False,
                    error="Protected file modification attempted"
                )

        temp_image = None
        temp_container = None

        try:
            # Step 1: Create a temporary image from current container state
            temp_image = f"debug-snapshot-{uuid.uuid4().hex[:8]}"
            commit_result = subprocess.run(
                ["docker", "commit", self.container_id, temp_image],
                capture_output=True,
                text=True,
                timeout=60
            )
            if commit_result.returncode != 0:
                return BashResult(
                    cwd=self.cwd,
                    stdout="",
                    stderr=f"Failed to create debug snapshot: {commit_result.stderr}",
                    success=False,
                    error="Debug snapshot failed"
                )

            # Step 2: Start a temporary container from the snapshot
            run_result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "--memory", DEFAULT_CONTAINER_MEMORY,
                    "--cpus", DEFAULT_CONTAINER_CPUS,
                    temp_image,
                    "tail", "-f", "/dev/null"  # Keep container running
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            if run_result.returncode != 0:
                return BashResult(
                    cwd=self.cwd,
                    stdout="",
                    stderr=f"Failed to start debug container: {run_result.stderr}",
                    success=False,
                    error="Debug container failed"
                )
            temp_container = run_result.stdout.strip()

            # Step 3: Enable write permissions (except test files)
            subprocess.run(
                ["docker", "exec", temp_container, "chmod", "-R", "u+w", REPO_ROOT],
                capture_output=True,
                timeout=60
            )

            # Re-protect test files
            for test_file in self.protected_test_files:
                subprocess.run(
                    ["docker", "exec", temp_container, "chmod", "a-w", f"{REPO_ROOT}/{test_file}"],
                    capture_output=True,
                    timeout=10
                )

            # Step 4: Apply patch if provided
            if patch and patch.strip():
                patch_file = f"{AGENT_TEMP_DIR}/debug_patch.diff"

                # Write patch to file
                write_proc = subprocess.run(
                    ["docker", "exec", "-i", temp_container, "tee", patch_file],
                    input=patch,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if write_proc.returncode == 0:
                    # Apply the patch
                    apply_result = subprocess.run(
                        ["docker", "exec", "-w", REPO_ROOT, temp_container,
                         "git", "apply", "--whitespace=fix", patch_file],
                        capture_output=True,
                        text=True,
                        timeout=60
                    )
                    if apply_result.returncode != 0:
                        return BashResult(
                            cwd=self.cwd,
                            stdout="",
                            stderr=f"Debug patch failed: {apply_result.stderr}",
                            success=False,
                            error="Debug patch failed"
                        )

            # Step 5: Execute the command
            exec_result = subprocess.run(
                ["docker", "exec", "-w", self.cwd, temp_container, "bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout
            )

            return BashResult(
                cwd=self.cwd,
                stdout=exec_result.stdout,
                stderr=exec_result.stderr,
                success=exec_result.returncode == 0
            )

        except subprocess.TimeoutExpired:
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr=f"Debug command timed out after {timeout}s",
                success=False,
                error="Timeout"
            )
        except Exception as e:
            return BashResult(
                cwd=self.cwd,
                stdout="",
                stderr=str(e),
                success=False,
                error=str(e)
            )
        finally:
            # Step 6: Cleanup - destroy temp container and image
            if temp_container:
                subprocess.run(
                    ["docker", "rm", "-f", temp_container],
                    capture_output=True,
                    timeout=30
                )
            if temp_image:
                subprocess.run(
                    ["docker", "rmi", "-f", temp_image],
                    capture_output=True,
                    timeout=30
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
