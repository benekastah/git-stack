class StackError(Exception):
    pass


class BrokenStackError(StackError):
    """A branch's metadata doesn't match the repository, so it can't be restacked."""


class LockHeldByCaller(StackError):
    """git-stack's lock is held by the git-stack process that started this
    one (e.g. through one of git's hooks), which waits for this one, so
    waiting for the lock would wait forever."""
