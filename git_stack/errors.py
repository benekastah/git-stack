class StackError(Exception):
    pass


class BrokenStackError(StackError):
    """A branch's metadata doesn't match the repository, so it can't be restacked."""
