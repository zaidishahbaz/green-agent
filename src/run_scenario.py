#!/usr/bin/env python3
"""
Run SWE-bench evaluation scenario.

This script orchestrates running both Green and Purple agents locally
and optionally triggers an evaluation.

Usage:
    uv run swebench-run scenarios/swebench/scenario.toml
    uv run swebench-run scenarios/swebench/scenario.toml --serve-only
    # LOCAL TESTING: uv run swebench-run --docker-test  (uncomment in code to enable)
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

try:
    import tomli
except ImportError:
    import tomllib as tomli


def parse_toml(path: str) -> dict:
    """Parse scenario TOML file."""
    with open(path, "rb") as f:
        return tomli.load(f)


async def wait_for_agent(url: str, timeout: float = 30.0) -> bool:
    """Wait for an agent to become healthy."""
    agent_card_url = f"{url.rstrip('/')}/.well-known/agent-card.json"
    start = asyncio.get_event_loop().time()

    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() - start < timeout:
            try:
                response = await client.get(agent_card_url, timeout=2.0)
                if response.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)

    return False


async def wait_for_agents(agents: list[tuple[str, str]], timeout: float = 30.0) -> bool:
    """Wait for all agents to become healthy."""
    tasks = [wait_for_agent(url, timeout) for name, url in agents]
    results = await asyncio.gather(*tasks)

    for (name, url), healthy in zip(agents, results):
        if healthy:
            print(f"  ✓ {name} ready at {url}")
        else:
            print(f"  ✗ {name} not responding at {url}")

    return all(results)


def start_process(command: str, cwd: str, show_logs: bool = False) -> subprocess.Popen:
    """Start a subprocess."""
    env = os.environ.copy()

    kwargs = {
        "shell": True,
        "cwd": cwd,
        "env": env,
        "start_new_session": True,
    }

    if not show_logs:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL

    return subprocess.Popen(command, **kwargs)


async def run_evaluation(green_url: str, config: dict) -> dict:
    """Run evaluation by sending request to green agent."""
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types import Message, Part, Role, TextPart
    from uuid import uuid4

    # Build request
    request = {
        "participants": {"solver": config.get("solver_url", "http://localhost:9010")},
        "config": {k: v for k, v in config.items() if k != "solver_url"},
    }

    async with httpx.AsyncClient(timeout=600) as http_client:
        resolver = A2ACardResolver(httpx_client=http_client, base_url=green_url)
        agent_card = await resolver.get_agent_card()

        client_config = ClientConfig(httpx_client=http_client, streaming=False)
        factory = ClientFactory(client_config)
        client = factory.create(agent_card)

        msg = Message(
            kind="message",
            role=Role.user,
            parts=[Part(TextPart(text=json.dumps(request)))],
            message_id=uuid4().hex,
        )

        print(f"\nSending evaluation request...")
        print(f"  Config: {request['config']}")

        events = []
        async for event in client.send_message(msg):
            events.append(event)

        return events


# =============================================================================
# LOCAL TESTING ONLY - Uncomment to use --docker-test for local validation
# =============================================================================
# def run_docker_test():
#     """Run a quick Docker validation test.
#
#     Note: This test now requires using ContainerExecutor to set up the environment
#     and apply the patch before running validation.
#     """
#     import asyncio
#
#     print("Running Docker validation test...")
#     print("=" * 50)
#
#     # Import here to avoid loading heavy deps for --help
#     sys.path.insert(0, str(Path(__file__).parent))
#     from docker_validator import DockerValidator
#     from swebench import SWEBenchDataset
#     from container_executor import ContainerExecutor
#
#     print("Loading SWE-bench dataset...")
#     dataset = SWEBenchDataset()
#     dataset.load()
#
#     task = dataset.get_task_by_id("sympy__sympy-20916")
#     if not task:
#         print("Error: Task not found")
#         return 1
#
#     print(f"\nTask: {task.instance_id}")
#     print(f"Repo: {task.repo}")
#     print(f"Tests: {len(task.fail_to_pass)}")
#
#     async def run_test():
#         # Set up container
#         print("\nStarting container...")
#         container = ContainerExecutor()
#         success, error = await container.start(task)
#         if not success:
#             print(f"Error starting container: {error}")
#             return 1
#
#         try:
#             # Apply gold patch
#             print("\nApplying gold patch...")
#             patch_result = await container.apply_patch(task.patch)
#             if not patch_result.success:
#                 print(f"Error applying patch: {patch_result.stderr}")
#                 return 1
#
#             # Run validation
#             print("\nRunning validation...")
#             validator = DockerValidator(container_id=container.container_id)
#             result = validator.validate_task(task)
#
#             print(f"\n{'=' * 50}")
#             print(f"RESULTS")
#             print(f"{'=' * 50}")
#             print(f"Patch applied:   {result.patch_applied}")
#             print(f"Install success: {result.install_success}")
#             print(f"Tests passed:    {result.tests_passed}/{result.tests_passed + result.tests_failed}")
#             print(f"Score:           {result.score:.0%}")
#
#             if result.errors:
#                 print(f"\nErrors: {result.errors}")
#
#             return 0 if result.score == 1.0 else 1
#
#         finally:
#             await container.stop()
#
#     return asyncio.run(run_test())
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description="Run SWE-bench evaluation scenario")
    parser.add_argument("scenario", nargs="?", help="Path to scenario.toml")
    parser.add_argument("--serve-only", action="store_true", help="Only start agents, don't run evaluation")
    parser.add_argument("--show-logs", action="store_true", help="Show agent logs")
    # LOCAL TESTING ONLY - Uncomment to enable --docker-test
    # parser.add_argument("--docker-test", action="store_true", help="Run Docker validation test")
    parser.add_argument("--timeout", type=float, default=30.0, help="Agent startup timeout")

    args = parser.parse_args()

    # LOCAL TESTING ONLY - Uncomment to enable --docker-test
    # if args.docker_test:
    #     sys.exit(run_docker_test())

    if not args.scenario:
        parser.print_help()
        sys.exit(1)

    # Parse scenario
    scenario_path = Path(args.scenario)
    if not scenario_path.exists():
        print(f"Error: Scenario file not found: {scenario_path}")
        sys.exit(1)

    scenario = parse_toml(str(scenario_path))
    scenario_dir = scenario_path.parent

    # Collect agents to start
    processes = []
    agents = []

    # Start participant agents (Purple Agent)
    for role, participant in scenario.get("participants", {}).items():
        url = participant["url"]
        command = participant["command"]
        cwd = scenario_dir / participant.get("cwd", ".")

        print(f"Starting {role} agent: {command}")
        proc = start_process(command, str(cwd), args.show_logs)
        processes.append(proc)
        agents.append((role, url))

    # Start green agent
    green = scenario.get("green", {})
    green_url = green.get("url", "http://localhost:9009")
    green_command = green.get("command", "uv run python -m server")
    green_cwd = scenario_dir / green.get("cwd", ".")

    print(f"Starting green agent: {green_command}")
    proc = start_process(green_command, str(green_cwd), args.show_logs)
    processes.append(proc)
    agents.append(("green", green_url))

    # Signal handler for cleanup
    def cleanup(sig=None, frame=None):
        print("\nShutting down agents...")
        for proc in processes:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # Wait for agents to be ready
    print(f"\nWaiting for agents (timeout: {args.timeout}s)...")
    ready = asyncio.run(wait_for_agents(agents, args.timeout))

    if not ready:
        print("\nError: Not all agents started successfully")
        cleanup()
        sys.exit(1)

    print("\nAll agents ready!")

    if args.serve_only:
        print("\n--serve-only mode: Press Ctrl+C to stop")
        try:
            while True:
                asyncio.get_event_loop().run_until_complete(asyncio.sleep(1))
        except KeyboardInterrupt:
            cleanup()
    else:
        # Run evaluation
        config = scenario.get("config", {})
        # Add solver URL from participants
        solver = scenario.get("participants", {}).get("solver", {})
        if solver:
            config["solver_url"] = solver["url"]

        try:
            events = asyncio.run(run_evaluation(green_url, config))
            print(f"\nReceived {len(events)} events from green agent")

            # Print results
            for event in events:
                if hasattr(event, "artifacts") and event.artifacts:
                    for artifact in event.artifacts:
                        print(f"\n{artifact.name}:")
                        for part in artifact.parts:
                            if hasattr(part.root, "text"):
                                print(part.root.text)
        except Exception as e:
            print(f"\nEvaluation error: {e}")

        cleanup()


if __name__ == "__main__":
    main()
