#!/usr/bin/env python3
"""
SWE-bench Test Runner for Docker containers.

This script runs inside a Docker container and:
1. Clones a repo at a specific commit
2. Applies a patch
3. Installs dependencies
4. Runs specified tests
5. Outputs results as JSON

Usage:
    echo '{"repo": "...", "base_commit": "...", "patch": "...", "tests": [...]}' | python run_tests.py

    Or with a file:
    python run_tests.py < task.json
"""

import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path


def run_command(cmd, cwd=None, timeout=300):
    """Run a command and return (return_code, stdout, stderr)."""
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


def clone_repo(repo, commit, target_dir):
    """Clone a repository at a specific commit."""
    url = f"https://github.com/{repo}.git"

    code, stdout, stderr = run_command(
        ["git", "clone", "--quiet", url, str(target_dir)],
        timeout=300
    )
    if code != 0:
        return False, f"Clone failed: {stderr}"

    code, stdout, stderr = run_command(
        ["git", "checkout", "--quiet", commit],
        cwd=target_dir
    )
    if code != 0:
        return False, f"Checkout failed: {stderr}"

    return True, ""


def apply_patch(patch, repo_dir):
    """Apply a git diff patch."""
    if not patch or not patch.strip():
        return False, "Empty patch"

    patch_file = repo_dir / "patch.diff"
    patch_file.write_text(patch)

    code, stdout, stderr = run_command(
        ["git", "apply", "--verbose", "patch.diff"],
        cwd=repo_dir
    )

    patch_file.unlink(missing_ok=True)

    if code != 0:
        # Try with --3way
        patch_file.write_text(patch)
        code, stdout, stderr = run_command(
            ["git", "apply", "--3way", "patch.diff"],
            cwd=repo_dir
        )
        patch_file.unlink(missing_ok=True)

        if code != 0:
            return False, f"Patch failed: {stderr}"

    return True, ""


def install_dependencies(repo_dir, timeout=600):
    """Install repo dependencies."""
    # Try different installation methods
    install_methods = [
        ["pip", "install", "-e", ".", "-q"],
        ["pip", "install", "-e", ".[dev]", "-q"],
        ["pip", "install", "-e", ".[test]", "-q"],
    ]

    for cmd in install_methods:
        code, stdout, stderr = run_command(cmd, cwd=repo_dir, timeout=timeout)
        if code == 0:
            return True, ""

    # Try requirements files
    req_files = ["requirements.txt", "requirements-dev.txt", "test-requirements.txt"]
    for req_file in req_files:
        if (repo_dir / req_file).exists():
            code, stdout, stderr = run_command(
                ["pip", "install", "-r", req_file, "-q"],
                cwd=repo_dir,
                timeout=timeout
            )
            if code == 0:
                return True, ""

    return False, "Could not install dependencies"


def detect_test_framework(repo_dir):
    """Detect which test framework the repo uses."""
    # Check for Django (uses tests/runtests.py)
    if (repo_dir / "django").exists() and (repo_dir / "tests" / "runtests.py").exists():
        return "django"
    # Check for pytest config
    if (repo_dir / "pytest.ini").exists() or (repo_dir / "pyproject.toml").exists():
        return "pytest"
    return "pytest"  # Default


def convert_unittest_to_django(test_name):
    """Convert unittest-style test name to Django runtests.py format.

    Input:  "test_method (module.ClassName)"
    Output: "module.ClassName.test_method"
    """
    import re
    match = re.match(r'(\w+)\s+\(([^)]+)\)', test_name)
    if match:
        method, path = match.groups()
        return f"{path}.{method}"
    return test_name


def convert_test_name_for_pytest(test_name):
    """Convert unittest-style test name to pytest format."""
    # Format: "test_method (module.ClassName)" -> "module.py::ClassName::test_method"
    import re
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


def run_test(test_name, repo_dir, timeout=120):
    """Run a single test and return result."""
    framework = detect_test_framework(repo_dir)

    if framework == "django":
        # Django uses tests/runtests.py
        django_test = convert_unittest_to_django(test_name)

        # Run Django tests using runtests.py
        code, stdout, stderr = run_command(
            ["python", "tests/runtests.py", django_test, "-v", "2"],
            cwd=repo_dir,
            timeout=timeout
        )
    else:
        # Use pytest
        pytest_test = convert_test_name_for_pytest(test_name)
        code, stdout, stderr = run_command(
            ["python", "-m", "pytest", pytest_test, "-xvs", "--tb=short"],
            cwd=repo_dir,
            timeout=timeout
        )

    return {
        "name": test_name,
        "passed": code == 0,
        "output": (stdout + stderr)[-2000:],  # Last 2000 chars
    }


def checkout_commit(commit, repo_dir):
    """Checkout a specific commit."""
    code, stdout, stderr = run_command(
        ["git", "checkout", "--quiet", commit],
        cwd=repo_dir
    )
    if code != 0:
        return False, f"Checkout failed: {stderr}"
    return True, ""


def main():
    # Read task from stdin
    try:
        task = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON input: {e}"}))
        sys.exit(1)

    # Extract task fields
    instance_id = task.get("instance_id", "unknown")
    repo = task.get("repo")
    base_commit = task.get("base_commit")
    environment_setup_commit = task.get("environment_setup_commit", base_commit)
    patch = task.get("patch", "")
    test_patch = task.get("test_patch", "")
    fail_to_pass = task.get("fail_to_pass", [])
    pass_to_pass = task.get("pass_to_pass", [])
    # Support legacy "tests" field for backward compatibility
    tests = task.get("tests", []) or (fail_to_pass + pass_to_pass)
    timeout_per_test = task.get("timeout_per_test", 120)

    if not repo or not base_commit:
        print(json.dumps({"error": "Missing required fields: repo, base_commit"}))
        sys.exit(1)

    result = {
        "instance_id": instance_id,
        "patch_applied": False,
        "install_success": False,
        "test_results": {},
        "errors": [],
    }

    # Create workspace
    workspace = Path("/workspace/repo")
    if workspace.exists():
        shutil.rmtree(workspace)

    # Step 1: Clone repo at environment_setup_commit (for installing dependencies)
    success, error = clone_repo(repo, environment_setup_commit, workspace)
    if not success:
        result["errors"].append(f"Clone: {error}")
        print(json.dumps(result))
        sys.exit(0)

    # Step 2: Install dependencies at environment_setup_commit
    success, error = install_dependencies(workspace)
    if not success:
        result["errors"].append(f"Install: {error}")
        # Continue anyway, some tests might work
    else:
        result["install_success"] = True

    # Step 3: Checkout to base_commit for evaluation
    if environment_setup_commit != base_commit:
        success, error = checkout_commit(base_commit, workspace)
        if not success:
            result["errors"].append(f"Checkout base_commit: {error}")
            print(json.dumps(result))
            sys.exit(0)

    # Step 4: Apply patch
    if patch:
        success, error = apply_patch(patch, workspace)
        if not success:
            result["errors"].append(f"Patch: {error}")
            print(json.dumps(result))
            sys.exit(0)
        result["patch_applied"] = True
    else:
        result["patch_applied"] = True  # No patch needed

    # Step 5: Apply test_patch (test file changes for evaluation)
    if test_patch:
        success, error = apply_patch(test_patch, workspace)
        if not success:
            result["errors"].append(f"Test patch: {error}")
            # Continue anyway, tests might still work

    # Step 6: Run tests
    fail_to_pass_results = {}
    pass_to_pass_results = {}

    for test in fail_to_pass:
        test_result = run_test(test, workspace, timeout=timeout_per_test)
        fail_to_pass_results[test] = test_result
        result["test_results"][test] = test_result

    for test in pass_to_pass:
        test_result = run_test(test, workspace, timeout=timeout_per_test)
        pass_to_pass_results[test] = test_result
        result["test_results"][test] = test_result

    result["fail_to_pass_results"] = fail_to_pass_results
    result["pass_to_pass_results"] = pass_to_pass_results

    # Calculate summary
    f2p_passed = sum(1 for r in fail_to_pass_results.values() if r["passed"])
    f2p_total = len(fail_to_pass_results)
    p2p_passed = sum(1 for r in pass_to_pass_results.values() if r["passed"])
    p2p_total = len(pass_to_pass_results)

    passed = f2p_passed + p2p_passed
    total = f2p_total + p2p_total
    result["summary"] = {
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

    print(json.dumps(result))


if __name__ == "__main__":
    main()
