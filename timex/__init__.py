"""TimeX: bounded, offline deliberation."""
__all__ = ["run_council"]


def __getattr__(name):
    # Read-only data tools can import TimeX without loading the deliberation stack.
    if name == 'run_council':
        from .graph import run_council
        return run_council
    raise AttributeError(name)
