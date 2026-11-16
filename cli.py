"""``hermes memorybank ...`` subcommands (wired in by Hermes while this
provider is the active ``memory.provider``). The implementation lives in
``core/cli.py`` so it also runs without Hermes through ``run.py``."""

from .core.cli import memorybank_command, register_cli  # noqa: F401
