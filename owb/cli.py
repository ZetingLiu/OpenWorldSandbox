"""OpenWorldSandbox CLI.

Commands:
    owb compile   — validate scenario+task JSON, compile to SQLite, replay walkthroughs
    owb env start — start the MCP environment server
    owb run       — run an agent against a compiled task
    owb verify    — verify an agent run against the goal DSL
    owb report    — aggregate capability-tag evaluation report
    owb sandbox   — interactive REPL for manually driving the world
    owb synth     — LLM synthesis pipeline (legacy, from awm)
"""

import importlib
from enum import Enum


class TopCmd(str, Enum):
    compile = "compile"
    env = "env"
    run = "run"
    verify = "verify"
    report = "report"
    sandbox = "sandbox"
    synth = "synth"


class EnvCmd(str, Enum):
    start = "start"


class SynthCmd(str, Enum):
    scenario = "scenario"
    task = "task"
    db = "db"
    sample = "sample"
    spec = "spec"
    env = "env"
    verifier = "verifier"
    all = "all"


def _build_commands() -> dict:
    from owb.env.compile import CompileConfig
    from owb.env.server import ServerConfig
    from owb.env.sandbox_cli import SandboxConfig
    from owb.run.runner import RunnerConfig
    from owb.eval.verify import VerifyConfig
    from owb.eval.report import ReportConfig

    synth_commands = {}
    try:
        from owb.synth.scenario import Config as ScenarioConfig
        from owb.synth.task import Config as TaskConfig
        from owb.synth.db import Config as DbConfig
        from owb.synth.sample import Config as SampleConfig
        from owb.synth.spec import Config as SpecConfig
        from owb.synth.env import Config as EnvGenConfig
        from owb.synth.verifier import Config as VerifierConfig
        from owb.synth.pipeline import Config as PipelineConfig

        synth_commands = {
            SynthCmd.scenario: ScenarioConfig,
            SynthCmd.task: TaskConfig,
            SynthCmd.db: DbConfig,
            SynthCmd.sample: SampleConfig,
            SynthCmd.spec: SpecConfig,
            SynthCmd.env: EnvGenConfig,
            SynthCmd.verifier: VerifierConfig,
            SynthCmd.all: PipelineConfig,
        }
    except ImportError:
        pass

    return {
        TopCmd.compile: CompileConfig,
        TopCmd.env: {
            EnvCmd.start: ServerConfig,
        },
        TopCmd.run: RunnerConfig,
        TopCmd.verify: VerifyConfig,
        TopCmd.report: ReportConfig,
        TopCmd.sandbox: SandboxConfig,
        TopCmd.synth: synth_commands,
    }


DISPATCH = {
    (TopCmd.compile,): "owb.env.compile",
    (TopCmd.env, EnvCmd.start): "owb.env.server",
    (TopCmd.run,): "owb.run.runner",
    (TopCmd.verify,): "owb.eval.verify",
    (TopCmd.report,): "owb.eval.report",
    (TopCmd.sandbox,): "owb.env.sandbox_cli",
    (TopCmd.synth, SynthCmd.scenario): "owb.synth.scenario",
    (TopCmd.synth, SynthCmd.task): "owb.synth.task",
    (TopCmd.synth, SynthCmd.db): "owb.synth.db",
    (TopCmd.synth, SynthCmd.sample): "owb.synth.sample",
    (TopCmd.synth, SynthCmd.spec): "owb.synth.spec",
    (TopCmd.synth, SynthCmd.env): "owb.synth.env",
    (TopCmd.synth, SynthCmd.verifier): "owb.synth.verifier",
    (TopCmd.synth, SynthCmd.all): "owb.synth.pipeline",
}


def main():
    # Load .env from the project root or any ancestor directory.
    # python-dotenv's default behaviour walks upward; do NOT pin to
    # Path.cwd() — that breaks invocation from subdirectories.
    from dotenv import load_dotenv
    load_dotenv()

    from simpleArgParser import parse_args_with_commands

    commands = _build_commands()
    command_path, config = parse_args_with_commands(
        commands,
        description="OpenWorldSandbox — Data-driven embodied agent sandbox",
    )

    module_name = DISPATCH.get(command_path)
    if module_name is None:
        print(f"Error: unknown command path {command_path}")
        exit(1)

    module = importlib.import_module(module_name)
    module.run(config)


if __name__ == "__main__":
    main()