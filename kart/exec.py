import os
import subprocess
import sys

from . import is_windows
from .cli_util import tool_environment


def _kart_no_exec(cmd, args, env):
    # In testing, .run must be set to capture_output and so use PIPEs to communicate
    # with the process to run whereas in normal operation the standard streams of
    # this process are passed into subprocess.run.
    # Capturing the output in a PIPE and then writing to sys.stdout is compatible
    # with click.testing which sets sys.stdout and sys.stderr to a custom
    # io wrapper.
    # This io wrapper is not compatible with the stdin= kwarg to .run - in that case
    # it gets treated as a file like object and fails.
    p = subprocess.run([cmd] + args[1:], capture_output=True, encoding="utf-8", env=env)
    sys.stdout.write(p.stdout)
    sys.stdout.flush()
    sys.stderr.write(p.stderr)
    sys.stderr.flush()
    sys.exit(p.returncode)


def run_and_wait(cmd, args):
    """
    run a process and wait for it to exit, this is required
    when in helper mode as execvpe overwrites the process so
    the caller can't be notified when the command is complete
    """
    env = tool_environment(os.environ)
    if "_KART_NO_EXEC" in os.environ:
        _kart_no_exec(cmd, args, env)
    else:
        p = subprocess.run(
            [cmd] + args[1:],
            encoding="utf-8",
            env=env,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        sys.exit(p.returncode)
