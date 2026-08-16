import contextlib
import signal

from .cli import main

# Piping into `head` closes the pipe: exit quietly like every other CLI instead of
# printing a BrokenPipeError traceback. Only the executable does this; importing
# the package must not change signal dispositions.
with contextlib.suppress(AttributeError, ValueError):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

raise SystemExit(main())
