"""OpenWorldSandbox CLI.

Commands:
    ows compile   — validate scenario+task JSON, compile to SQLite, replay walkthroughs
    ows env start — start the MCP environment server
    ows run       — run an agent against a compiled task
    ows verify    — verify an agent run against the goal DSL
    ows report    — aggregate capability-tag evaluation report
    ows sandbox   — interactive REPL for manually driving the world
    ows synth     — LLM synthesis pipeline (legacy, from awm)
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
    gen = "gen"


class EnvCmd(str, Enum):
    start = "start"


class GenCmd(str, Enum):
    run = "run"
    report = "report"


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
    from ows.env.compile import CompileConfig
    from ows.env.server import ServerConfig
    from ows.env.sandbox_cli import SandboxConfig
    from ows.run.runner import RunnerConfig
    from ows.eval.verify import VerifyConfig
    from ows.eval.report import ReportConfig

    synth_commands = {}
    try:
        from ows.synth.scenario import Config as ScenarioConfig
        from ows.synth.task import Config as TaskConfig
        from ows.synth.db import Config as DbConfig
        from ows.synth.sample import Config as SampleConfig
        from ows.synth.spec import Config as SpecConfig
        from ows.synth.env import Config as EnvGenConfig
        from ows.synth.verifier import Config as VerifierConfig
        from ows.synth.pipeline import Config as PipelineConfig

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

    gen_commands = {}
    try:
        from ows.gen.pipeline import Config as GenConfig
        from ows.gen.report import Config as GenReportConfig

        gen_commands = {
            GenCmd.run: GenConfig,
            GenCmd.report: GenReportConfig,
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
        TopCmd.gen: gen_commands,
    }


DISPATCH = {
    (TopCmd.compile,): "ows.env.compile",
    (TopCmd.env, EnvCmd.start): "ows.env.server",
    (TopCmd.run,): "ows.run.runner",
    (TopCmd.verify,): "ows.eval.verify",
    (TopCmd.report,): "ows.eval.report",
    (TopCmd.sandbox,): "ows.env.sandbox_cli",
    (TopCmd.synth, SynthCmd.scenario): "ows.synth.scenario",
    (TopCmd.synth, SynthCmd.task): "ows.synth.task",
    (TopCmd.synth, SynthCmd.db): "ows.synth.db",
    (TopCmd.synth, SynthCmd.sample): "ows.synth.sample",
    (TopCmd.synth, SynthCmd.spec): "ows.synth.spec",
    (TopCmd.synth, SynthCmd.env): "ows.synth.env",
    (TopCmd.synth, SynthCmd.verifier): "ows.synth.verifier",
    (TopCmd.synth, SynthCmd.all): "ows.synth.pipeline",
    (TopCmd.gen, GenCmd.run): "ows.gen.pipeline",
    (TopCmd.gen, GenCmd.report): "ows.gen.report",
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
        description="OpenWorldSandbox — embodied scene sandbox for household and retail worlds",
    )

    module_name = DISPATCH.get(command_path)
    if module_name is None:
        print(f"Error: unknown command path {command_path}")
        exit(1)

    module = importlib.import_module(module_name)
    module.run(config)


if __name__ == "__main__":
    main()