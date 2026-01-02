#!/usr/bin/env python3
"""Test script for the SWE-bench Green Agent."""

import argparse
import asyncio
import json
import sys
sys.path.insert(0, 'src')

from messenger import send_message


DEFAULT_GREEN_AGENT = "http://127.0.0.1:9009/"
DEFAULT_PURPLE_AGENT = "http://127.0.0.1:9010/"


async def run_test(
    green_url: str,
    solver_url: str,
    instance_id: str | None = None,
    repo: str | None = None,
    difficulty: str | None = None,
    max_tasks: int = 1,
    timeout: int = 120,
    verbose: bool = False,
):
    """Send a test request to the Green Agent."""

    config = {"max_tasks": max_tasks}
    if instance_id:
        config["instance_id"] = instance_id
    if repo:
        config["repo"] = repo
    if difficulty:
        config["difficulty"] = difficulty

    request = {
        "participants": {"solver": solver_url},
        "config": config,
    }

    print(f"Green Agent: {green_url}")
    print(f"Solver Agent: {solver_url}")
    print(f"Config: {json.dumps(config)}")
    print("-" * 50)

    result = await send_message(
        message=json.dumps(request),
        base_url=green_url,
        timeout=timeout,
    )

    status = result.get("status", "N/A")
    response = result.get("response", "No response")

    print(f"Status: {status}")
    print()

    if verbose:
        print(response)
    else:
        # Parse and pretty print summary
        lines = response.split("\n")
        print(lines[0])  # Summary line

        # Try to parse the JSON data
        try:
            json_start = response.find("{")
            if json_start >= 0:
                data = json.loads(response[json_start:])
                print(f"\nTotal: {data['total_tasks']} | Completed: {data['completed']} | Failed: {data['failed']}")
                print("\nTasks:")
                for r in data.get("results", []):
                    status_icon = "✓" if r['status'] == "completed" else "✗"
                    print(f"  {status_icon} {r['instance_id']} ({r['repo']})")

                    # Show Purple Agent interaction
                    if r.get("solver_response"):
                        response_preview = r["solver_response"][:100].replace("\n", " ")
                        print(f"    └─ Purple Agent responded: {response_preview}...")
                    elif r.get("error"):
                        print(f"    └─ Error: {r['error']}")
        except json.JSONDecodeError:
            print(response[:500])

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Test the SWE-bench Green Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with default settings (1 task)
  python scripts/test_agent.py

  # Test a specific task
  python scripts/test_agent.py --instance-id django__django-11099

  # Test multiple tasks from a repo
  python scripts/test_agent.py --repo pytest-dev/pytest --max-tasks 3

  # Test with verbose output
  python scripts/test_agent.py --instance-id astropy__astropy-12907 -v
        """,
    )

    parser.add_argument(
        "--green-url",
        default=DEFAULT_GREEN_AGENT,
        help=f"Green Agent URL (default: {DEFAULT_GREEN_AGENT})",
    )
    parser.add_argument(
        "--solver-url",
        default=DEFAULT_PURPLE_AGENT,
        help=f"Solver/Purple Agent URL (default: {DEFAULT_PURPLE_AGENT})",
    )
    parser.add_argument(
        "--instance-id",
        help="Specific task instance ID (e.g., django__django-11099)",
    )
    parser.add_argument(
        "--repo",
        help="Filter by repository (e.g., pytest-dev/pytest)",
    )
    parser.add_argument(
        "--difficulty",
        help="Filter by difficulty level",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=1,
        help="Maximum number of tasks to run (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Request timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show full response output",
    )

    args = parser.parse_args()

    asyncio.run(
        run_test(
            green_url=args.green_url,
            solver_url=args.solver_url,
            instance_id=args.instance_id,
            repo=args.repo,
            difficulty=args.difficulty,
            max_tasks=args.max_tasks,
            timeout=args.timeout,
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
