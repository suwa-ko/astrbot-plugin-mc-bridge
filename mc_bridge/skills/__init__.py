"""Domain-split L1/L2 survival skills."""
from .base import SkillsBaseMixin
from .building import BuildingMixin
from .common import *  # noqa: F401,F403
from .farming import FarmingMixin
from .gathering import GatheringMixin
from .mining import MiningAndNavMixin
from .production import ProductionMixin
from .survival_ops import SurvivalOpsMixin


class VanillaSkills(
    SkillsBaseMixin,
    GatheringMixin,
    ProductionMixin,
    BuildingMixin,
    FarmingMixin,
    MiningAndNavMixin,
    SurvivalOpsMixin,
):
    """Composable facade exposing all L1/L2 Minecraft survival skills."""
