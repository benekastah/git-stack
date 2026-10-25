import enum


class MergeState(enum.Enum):
    NOT_MERGED = "not merged"
    MERGED = "merged"
    # Merged, but the branch or its remote branch has commits that weren't.
    MERGED_WITH_NEW_COMMITS = "merged, with new commits"
    # Merged, then some or all of its changes were reverted in the parent.
    REVERTED = "merged, then reverted"
