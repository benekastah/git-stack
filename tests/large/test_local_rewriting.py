"""The scenarios of tests/test_operations_local_rewriting.py in the large repository, with the counts they declare
there: the operations a command runs don't depend on how big the repository is, or how many
branches it has."""

import pytest

from tests import test_operations_local_rewriting as small

pytestmark = pytest.mark.usefixtures("separate_processes")

globals().update({name: test for name, test in vars(small).items() if name.startswith("test_")})
