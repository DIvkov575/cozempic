"""Pruning strategies for Cozempic.

Importing this package registers all strategies with the global registry.
"""

from . import gentle, standard, aggressive  # noqa: F401
from . import asset_offload  # noqa: F401  (registers @strategy)
from . import thinking_distill  # noqa: F401  (registers @strategy)
