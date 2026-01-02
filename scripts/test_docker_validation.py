#!/usr/bin/env python3
"""Quick test script for Docker-based validation."""

import sys
sys.path.insert(0, "src")

from docker_validator import DockerValidator
from swebench import SWEBenchDataset


def main():
    # Default to Django task
    instance_id = sys.argv[1] if len(sys.argv) > 1 else "django__django-11099"

    print(f"Loading SWE-bench dataset...")
    dataset = SWEBenchDataset()
    dataset.load()

    task = dataset.get_task_by_id(instance_id)
    if not task:
        print(f"Task not found: {instance_id}")
        return 1

    print(f"\nTask: {task.instance_id}")
    print(f"Repo: {task.repo}")
    print(f"Tests to pass: {len(task.fail_to_pass)}")
    for t in task.fail_to_pass:
        print(f"  - {t}")

    print(f"\nRunning Docker validation with gold patch...")
    validator = DockerValidator()
    result = validator.validate_task(task, task.patch)

    print(f"\n{'='*50}")
    print(f"RESULTS")
    print(f"{'='*50}")
    print(f"Patch applied:   {result.patch_applied}")
    print(f"Install success: {result.install_success}")
    print(f"Tests passed:    {result.tests_passed}")
    print(f"Tests failed:    {result.tests_failed}")
    print(f"Score:           {result.score:.0%}")

    if result.errors:
        print(f"\nErrors:")
        for e in result.errors:
            print(f"  - {e}")

    if result.test_results:
        print(f"\nTest Details:")
        for name, details in result.test_results.items():
            status = "PASS" if details.get("passed") else "FAIL"
            print(f"  [{status}] {name}")

    return 0 if result.score == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
