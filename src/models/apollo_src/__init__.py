"""Vendored Apollo model source (inference only).

Apollo: Band-sequence Modeling for High-Quality Audio Restoration (ICASSP 2025)
Source: https://github.com/JusperLee/Apollo (look2hear/models/)
Authors: Look2Hear, Tsinghua University
License: CC-BY-SA 4.0

Only the model definition is vendored — training/data/discriminator code is omitted.
"""

from .apollo import Apollo
from .base_model import BaseModel

__all__ = ["Apollo", "BaseModel"]
