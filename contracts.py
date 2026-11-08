"""L0 instruction contracts.

This module is deliberately dependency-free.  It is the single source of truth
for L0 opcode names, resource declarations and request/result validation.  The
current plugin can adopt it incrementally; importing it does not start any task.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Layer(int, Enum):
    L0 = 0
    L1 = 1
    L2 = 2


class ErrorCode(str, Enum):
    OK = "OK"
    INVALID_PARAM = "INVALID_PARAM"
    NOT_FOUND = "NOT_FOUND"
    OUT_OF_REACH = "OUT_OF_REACH"
    NO_TOOL = "NO_TOOL"
    PROTECTED = "PROTECTED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    QUEUE_FULL = "QUEUE_FULL"
    CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"
    WORLD_CHANGED = "WORLD_CHANGED"
    UNKNOWN = "UNKNOWN"


class Resource(str, Enum):
    ACTOR = "actor"
    CAMERA = "camera"
    RENDER = "render"
    INVENTORY = "inventory"
    CONTAINER = "container"
    NAVIGATION = "navigation"
    CHAT = "chat"


class Opcode(str, Enum):
    # Query / observation
    GET_STATUS = "get_status"
    GET_INVENTORY = "get_inventory"
    INSPECT_BLOCK = "inspect_block"
    SCAN_BLOCKS = "scan_blocks"
    SCAN_ENTITIES = "scan_entities"
    GET_TARGET = "get_target"
    GET_BLOCK_CONTEXT = "get_block_context"
    GET_MENU = "get_menu"
    QUERY_KNOWLEDGE = "query_knowledge"
    SCREENSHOT = "screenshot"

    # Camera
    LOOK_AT = "look_at"

    # Movement
    NAVIGATE_TO = "navigate_to"
    STOP_MOVEMENT = "stop_movement"
    FOLLOW_ENTITY = "follow_entity"
    ALIGN_TO = "align_to"
    JUMP_ONCE = "jump_once"
    RIDE_ENTITY = "ride_entity"
    DISMOUNT = "dismount"

    # Inventory / item
    SELECT_HOTBAR = "select_hotbar"
    SWAP_INVENTORY = "swap_inventory"
    DROP_ITEM = "drop_item"
    PICKUP_ENTITY = "pickup_entity"
    EQUIP_ARMOR = "equip_armor"
    UNEQUIP_ARMOR = "unequip_armor"
    USE_ITEM = "use_item"
    USE_ITEM_ON_BLOCK = "use_item_on_block"
    USE_ITEM_ON_ENTITY = "use_item_on_entity"
    RELEASE_USE = "release_use"
    OPEN_CONTAINER = "open_container"
    READ_CONTAINER = "read_container"
    CLICK_SLOT = "click_slot"
    CLOSE_CONTAINER = "close_container"

    # Blocks / interaction
    MINE_BLOCK = "mine_block"
    MINE_BELOW = "mine_below"
    PLACE_BLOCK = "place_block"
    INTERACT_BLOCK = "interact_block"
    PLANT_SEED = "plant_seed"
    TILL_BLOCK = "till_block"
    USE_BONEMEAL = "use_bonemeal"
    ATTACK_ENTITY = "attack_entity"
    INTERACT_ENTITY = "interact_entity"
    WAKE_UP = "wake_up"

    # Chat / memory / control
    SEND_CHAT = "send_chat"
    MEMORY_GET = "memory_get"
    MEMORY_SET = "memory_set"
    MEMORY_FORGET = "memory_forget"
    MEMORY_LIST = "memory_list"
    WAIT = "wait"


class L1Opcode(str, Enum):
    SLEEP_AT = "sleep_at"
    CRAFT_RECIPE = "craft_recipe"
    SMELT_BATCH = "smelt_batch"
    TRANSFER_ITEM = "transfer_item"
    ATTACK_TARGET = "attack_target"
    EQUIP_BEST = "equip_best"
    EAT_UNTIL_FULL = "eat_until_full"
    HARVEST_BLOCK = "harvest_block"
    PLANT_AT = "plant_at"
    TILL_AND_PLANT = "till_and_plant"
    OPEN_AND_READ = "open_and_read"
    PLACE_LINE = "place_line"
    RETURN_TO = "return_to"


class L2Opcode(str, Enum):
    GATHER = "gather"
    MINE_ORE = "mine_ore"
    CRAFT_ITEM = "craft_item"
    SMELT_ITEM = "smelt_item"
    CONTAINER_TRANSFER = "container_transfer"
    BUILD = "build"
    FARM = "farm"
    RESUPPLY = "resupply"
    EXPLORE = "explore"
    COMBAT = "combat"
    TRADE_FOR = "trade_for"
    FIND_AND_SLEEP = "find_and_sleep"
    RETURN_HOME = "return_home"


# Kept explicit so the boundary is reviewable and testable.
L1_ONLY = frozenset({
    "dig_down",
    "sleep_at",
    "craft_recipe",
    "smelt_batch",
    "transfer_item",
    "attack_target",
    "equip_best",
    "eat_until_full",
    "harvest_block",
    "plant_at",
    "till_and_plant",
    "open_and_read",
    "place_line",
    "return_to",
})

L2_ONLY = frozenset({
    "gather",
    "mine_ore",
    "craft_item",
    "smelt_item",
    "container_transfer",
    "build",
    "farm",
    "resupply",
    "explore",
    "combat",
    "trade_for",
    "find_and_sleep",
    "return_home",
})

KERNEL_ONLY = frozenset({
    "stop_actions",
    "respawn",
    "cancel_task",
    "preempt_task",
})


@dataclass(frozen=True)
class L0Spec:
    opcode: Opcode
    readonly: bool
    resources: frozenset[Resource]
    actor_required: bool
    idempotent: bool
    long_running: bool = False
    implemented: bool = True


def _spec(opcode, *, readonly, resources=(), actor=False, idempotent=True, long=False, implemented=True):
    return L0Spec(
        opcode=opcode,
        readonly=readonly,
        resources=frozenset(resources),
        actor_required=actor,
        idempotent=idempotent,
        long_running=long,
        implemented=implemented,
    )


L0_SPECS: dict[Opcode, L0Spec] = {
    # Query / observation
    Opcode.GET_STATUS: _spec(Opcode.GET_STATUS, readonly=True),
    Opcode.GET_INVENTORY: _spec(Opcode.GET_INVENTORY, readonly=True),
    Opcode.INSPECT_BLOCK: _spec(Opcode.INSPECT_BLOCK, readonly=True),
    Opcode.SCAN_BLOCKS: _spec(Opcode.SCAN_BLOCKS, readonly=True),
    Opcode.SCAN_ENTITIES: _spec(Opcode.SCAN_ENTITIES, readonly=True),
    Opcode.GET_TARGET: _spec(Opcode.GET_TARGET, readonly=True),
    Opcode.GET_BLOCK_CONTEXT: _spec(Opcode.GET_BLOCK_CONTEXT, readonly=True),
    Opcode.GET_MENU: _spec(Opcode.GET_MENU, readonly=True),
    Opcode.QUERY_KNOWLEDGE: _spec(Opcode.QUERY_KNOWLEDGE, readonly=True),
    Opcode.SCREENSHOT: _spec(Opcode.SCREENSHOT, readonly=True, resources=(Resource.RENDER,)),

    # Camera mutation
    Opcode.LOOK_AT: _spec(Opcode.LOOK_AT, readonly=False, resources=(Resource.CAMERA,), idempotent=True),

    # Movement
    Opcode.NAVIGATE_TO: _spec(Opcode.NAVIGATE_TO, readonly=False, resources=(Resource.ACTOR, Resource.NAVIGATION), actor=True, long=True),
    Opcode.STOP_MOVEMENT: _spec(Opcode.STOP_MOVEMENT, readonly=False, resources=(Resource.ACTOR, Resource.NAVIGATION), actor=True),
    Opcode.FOLLOW_ENTITY: _spec(Opcode.FOLLOW_ENTITY, readonly=False, resources=(Resource.ACTOR, Resource.NAVIGATION), actor=True, idempotent=False, long=True),
    Opcode.ALIGN_TO: _spec(Opcode.ALIGN_TO, readonly=False, resources=(Resource.ACTOR, Resource.NAVIGATION), actor=True),
    Opcode.JUMP_ONCE: _spec(Opcode.JUMP_ONCE, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.RIDE_ENTITY: _spec(Opcode.RIDE_ENTITY, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.DISMOUNT: _spec(Opcode.DISMOUNT, readonly=False, resources=(Resource.ACTOR,), actor=True),

    # Inventory / item
    Opcode.SELECT_HOTBAR: _spec(Opcode.SELECT_HOTBAR, readonly=False, resources=(Resource.ACTOR, Resource.INVENTORY), actor=True),
    Opcode.SWAP_INVENTORY: _spec(Opcode.SWAP_INVENTORY, readonly=False, resources=(Resource.ACTOR, Resource.INVENTORY), actor=True, idempotent=False),
    Opcode.DROP_ITEM: _spec(Opcode.DROP_ITEM, readonly=False, resources=(Resource.ACTOR, Resource.INVENTORY), actor=True, idempotent=False),
    Opcode.PICKUP_ENTITY: _spec(Opcode.PICKUP_ENTITY, readonly=False, resources=(Resource.ACTOR, Resource.INVENTORY), actor=True, idempotent=False),
    Opcode.EQUIP_ARMOR: _spec(Opcode.EQUIP_ARMOR, readonly=False, resources=(Resource.ACTOR, Resource.INVENTORY), actor=True, idempotent=False),
    Opcode.UNEQUIP_ARMOR: _spec(Opcode.UNEQUIP_ARMOR, readonly=False, resources=(Resource.ACTOR, Resource.INVENTORY), actor=True, idempotent=False),
    Opcode.USE_ITEM: _spec(Opcode.USE_ITEM, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.USE_ITEM_ON_BLOCK: _spec(Opcode.USE_ITEM_ON_BLOCK, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.USE_ITEM_ON_ENTITY: _spec(Opcode.USE_ITEM_ON_ENTITY, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.RELEASE_USE: _spec(Opcode.RELEASE_USE, readonly=False, resources=(Resource.ACTOR,), actor=True),
    Opcode.OPEN_CONTAINER: _spec(Opcode.OPEN_CONTAINER, readonly=False, resources=(Resource.ACTOR, Resource.CONTAINER), actor=True, idempotent=False),
    Opcode.READ_CONTAINER: _spec(Opcode.READ_CONTAINER, readonly=True, resources=(Resource.CONTAINER,)),
    Opcode.CLICK_SLOT: _spec(Opcode.CLICK_SLOT, readonly=False, resources=(Resource.ACTOR, Resource.CONTAINER), actor=True, idempotent=False),
    Opcode.CLOSE_CONTAINER: _spec(Opcode.CLOSE_CONTAINER, readonly=False, resources=(Resource.ACTOR, Resource.CONTAINER), actor=True),

    # Blocks / interaction
    Opcode.MINE_BLOCK: _spec(Opcode.MINE_BLOCK, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.MINE_BELOW: _spec(Opcode.MINE_BELOW, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.PLACE_BLOCK: _spec(Opcode.PLACE_BLOCK, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.INTERACT_BLOCK: _spec(Opcode.INTERACT_BLOCK, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.PLANT_SEED: _spec(Opcode.PLANT_SEED, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.TILL_BLOCK: _spec(Opcode.TILL_BLOCK, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.USE_BONEMEAL: _spec(Opcode.USE_BONEMEAL, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.ATTACK_ENTITY: _spec(Opcode.ATTACK_ENTITY, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.INTERACT_ENTITY: _spec(Opcode.INTERACT_ENTITY, readonly=False, resources=(Resource.ACTOR,), actor=True, idempotent=False),
    Opcode.WAKE_UP: _spec(Opcode.WAKE_UP, readonly=False, resources=(Resource.ACTOR,), actor=True),

    # Chat / memory / control
    Opcode.SEND_CHAT: _spec(Opcode.SEND_CHAT, readonly=False, resources=(Resource.CHAT,), idempotent=False),
    Opcode.MEMORY_GET: _spec(Opcode.MEMORY_GET, readonly=True),
    Opcode.MEMORY_SET: _spec(Opcode.MEMORY_SET, readonly=False, idempotent=True),
    Opcode.MEMORY_FORGET: _spec(Opcode.MEMORY_FORGET, readonly=False, idempotent=True),
    Opcode.MEMORY_LIST: _spec(Opcode.MEMORY_LIST, readonly=True),
    Opcode.WAIT: _spec(Opcode.WAIT, readonly=True),
}


@dataclass(frozen=True)
class L1Spec:
    opcode: L1Opcode
    resources: frozenset[Resource]
    max_seconds: int
    implemented: bool = True


L1_SPECS: dict[L1Opcode, L1Spec] = {
    L1Opcode.SLEEP_AT: L1Spec(L1Opcode.SLEEP_AT, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 30),
    L1Opcode.CRAFT_RECIPE: L1Spec(L1Opcode.CRAFT_RECIPE, frozenset({Resource.ACTOR, Resource.CONTAINER}), 30),
    L1Opcode.SMELT_BATCH: L1Spec(L1Opcode.SMELT_BATCH, frozenset({Resource.ACTOR, Resource.CONTAINER}), 180),
    L1Opcode.TRANSFER_ITEM: L1Spec(L1Opcode.TRANSFER_ITEM, frozenset({Resource.ACTOR, Resource.CONTAINER}), 30),
    L1Opcode.ATTACK_TARGET: L1Spec(L1Opcode.ATTACK_TARGET, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 30),
    L1Opcode.EQUIP_BEST: L1Spec(L1Opcode.EQUIP_BEST, frozenset({Resource.ACTOR, Resource.INVENTORY}), 10),
    L1Opcode.EAT_UNTIL_FULL: L1Spec(L1Opcode.EAT_UNTIL_FULL, frozenset({Resource.ACTOR, Resource.INVENTORY}), 30),
    L1Opcode.HARVEST_BLOCK: L1Spec(L1Opcode.HARVEST_BLOCK, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 20),
    L1Opcode.PLANT_AT: L1Spec(L1Opcode.PLANT_AT, frozenset({Resource.ACTOR}), 10),
    L1Opcode.TILL_AND_PLANT: L1Spec(L1Opcode.TILL_AND_PLANT, frozenset({Resource.ACTOR}), 15),
    L1Opcode.OPEN_AND_READ: L1Spec(L1Opcode.OPEN_AND_READ, frozenset({Resource.ACTOR, Resource.CONTAINER}), 20),
    L1Opcode.PLACE_LINE: L1Spec(L1Opcode.PLACE_LINE, frozenset({Resource.ACTOR}), 60),
    L1Opcode.RETURN_TO: L1Spec(L1Opcode.RETURN_TO, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 60),
}


@dataclass(frozen=True)
class L2Spec:
    opcode: L2Opcode
    resources: frozenset[Resource]
    max_seconds: int
    implemented: bool = True


L2_SPECS: dict[L2Opcode, L2Spec] = {
    L2Opcode.GATHER: L2Spec(L2Opcode.GATHER, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 600),
    L2Opcode.MINE_ORE: L2Spec(L2Opcode.MINE_ORE, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 600),
    L2Opcode.CRAFT_ITEM: L2Spec(L2Opcode.CRAFT_ITEM, frozenset({Resource.ACTOR, Resource.NAVIGATION, Resource.CONTAINER, Resource.INVENTORY}), 300),
    L2Opcode.SMELT_ITEM: L2Spec(L2Opcode.SMELT_ITEM, frozenset({Resource.ACTOR, Resource.NAVIGATION, Resource.CONTAINER}), 300),
    L2Opcode.CONTAINER_TRANSFER: L2Spec(L2Opcode.CONTAINER_TRANSFER, frozenset({Resource.ACTOR, Resource.NAVIGATION, Resource.CONTAINER}), 120),
    L2Opcode.BUILD: L2Spec(L2Opcode.BUILD, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 300),
    L2Opcode.FARM: L2Spec(L2Opcode.FARM, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 300),
    L2Opcode.RESUPPLY: L2Spec(L2Opcode.RESUPPLY, frozenset({Resource.ACTOR, Resource.NAVIGATION, Resource.CONTAINER}), 600),
    L2Opcode.EXPLORE: L2Spec(L2Opcode.EXPLORE, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 300),
    L2Opcode.COMBAT: L2Spec(L2Opcode.COMBAT, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 120),
    L2Opcode.TRADE_FOR: L2Spec(L2Opcode.TRADE_FOR, frozenset({Resource.ACTOR, Resource.NAVIGATION, Resource.CONTAINER}), 120),
    L2Opcode.FIND_AND_SLEEP: L2Spec(L2Opcode.FIND_AND_SLEEP, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 60),
    L2Opcode.RETURN_HOME: L2Spec(L2Opcode.RETURN_HOME, frozenset({Resource.ACTOR, Resource.NAVIGATION}), 180),
}

L1_OPCODE_NAMES = frozenset(opcode.value for opcode in L1Opcode)
L2_OPCODE_NAMES = frozenset(opcode.value for opcode in L2Opcode)


@dataclass
class L0Request:
    opcode: Opcode | str
    params: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 10000
    cancel_token: str = ""
    expected_effect: str = ""
    resources: frozenset[Resource] = field(default_factory=frozenset)
    idempotent: bool | None = None

    def validation_errors(self) -> list[str]:
        errors = []
        try:
            opcode = Opcode(self.opcode.value if isinstance(self.opcode, Opcode) else str(self.opcode))
        except ValueError:
            return [f"unknown opcode: {self.opcode}"]
        spec = L0_SPECS[opcode]
        if not isinstance(self.params, dict):
            errors.append("params must be an object")
        if not 1 <= int(self.timeout_ms) <= 300000:
            errors.append("timeout_ms must be 1..300000")
        declared = {Resource(item.value if isinstance(item, Resource) else str(item)) for item in self.resources}
        if spec.actor_required and Resource.ACTOR not in declared:
            errors.append("actor resource is required")
        if not spec.actor_required and Resource.ACTOR in declared:
            errors.append("actor resource must not be declared for this opcode")
        if spec.readonly and declared - spec.resources:
            errors.append("read-only opcode declares a mutating resource")
        return errors

    def validate(self) -> None:
        errors = self.validation_errors()
        if errors:
            raise ValueError("; ".join(errors))


@dataclass
class L0Result:
    ok: bool
    code: ErrorCode | str = ErrorCode.OK
    data: dict[str, Any] = field(default_factory=dict)
    state_delta: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    elapsed_ms: int = 0
    error: str = ""

    def __post_init__(self):
        self.code = ErrorCode(self.code.value if isinstance(self.code, ErrorCode) else str(self.code))


L0_OPCODE_NAMES = frozenset(opcode.value for opcode in Opcode)
