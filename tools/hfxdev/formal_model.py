# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .model import ModelError, load_json, require_unique


EXPECTED_INVARIANTS = {
    "HFX-MODEL-001",
    "HFX-MODEL-002",
    "HFX-MODEL-003",
    "HFX-MODEL-004",
    "HFX-MODEL-005",
    "HFX-MODEL-006",
    "HFX-MODEL-007",
    "HFX-MODEL-008",
}
NONTERMINAL = {"queued", "authorized", "reserved"}
TERMINAL = {"success", "safe-failure", "uncertain"}
OWNERS = {"none", "application", "restoration"}
PHASES = {"none", *NONTERMINAL, *TERMINAL}


@dataclass(frozen=True)
class FormalModel:
    maximum_depth: int
    maximum_states: int
    receiver_generations: int
    invariant_ids: tuple[str, ...]
    required_transitions: tuple[str, ...]


@dataclass(frozen=True)
class ModelState:
    issued_generation: int = 0
    active_generation: int = 0
    retired_mask: int = 0
    qualified: bool = False
    lease_owner: str = "none"
    lease_generation: int = 0
    transaction_phase: str = "none"
    transaction_generation: int = 0
    hardware_attempts: int = 0
    stable_intent_generation: int = 0


@dataclass(frozen=True)
class ModelResult:
    states: int
    transitions: int
    maximum_depth_reached: int
    transition_names: tuple[str, ...]


Transition = tuple[str, ModelState]


def load_formal_model(root: Path) -> FormalModel:
    value = load_json(root / "assurance" / "formal-model.json")
    if set(value) != {"$schema", "schema", "bounds", "invariants", "required_transitions"}:
        raise ModelError("formal model has missing or unknown top-level fields")
    if value["schema"] != "hyperflux-formal-model-v1":
        raise ModelError("unsupported formal-model schema")
    bounds = value["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != {
        "maximum_depth",
        "maximum_states",
        "receiver_generations",
    }:
        raise ModelError("formal model bounds are malformed")
    maximum_depth = bounds["maximum_depth"]
    maximum_states = bounds["maximum_states"]
    generations = bounds["receiver_generations"]
    if (
        isinstance(maximum_depth, bool)
        or not isinstance(maximum_depth, int)
        or not 1 <= maximum_depth <= 32
        or isinstance(maximum_states, bool)
        or not isinstance(maximum_states, int)
        or not 1 <= maximum_states <= 1_000_000
        or generations != 2
    ):
        raise ModelError("formal model bounds exceed the reviewed state space")
    invariants = value["invariants"]
    if not isinstance(invariants, list) or not invariants:
        raise ModelError("formal model must declare invariants")
    invariant_ids: list[str] = []
    for index, item in enumerate(invariants):
        if not isinstance(item, dict) or set(item) != {"id", "statement"}:
            raise ModelError(f"formal model invariant {index}: malformed")
        if not isinstance(item["id"], str) or not isinstance(item["statement"], str) or not item["statement"].strip():
            raise ModelError(f"formal model invariant {index}: invalid identity or statement")
        invariant_ids.append(item["id"])
    require_unique(invariant_ids, "formal model invariant id")
    if set(invariant_ids) != EXPECTED_INVARIANTS:
        raise ModelError("formal model invariant catalog differs from the executable checker")
    required = value["required_transitions"]
    if not isinstance(required, list) or not required or not all(isinstance(item, str) for item in required):
        raise ModelError("formal model required transitions must be a non-empty string array")
    require_unique(required, "formal model required transition")
    return FormalModel(
        maximum_depth=maximum_depth,
        maximum_states=maximum_states,
        receiver_generations=generations,
        invariant_ids=tuple(invariant_ids),
        required_transitions=tuple(required),
    )


def _retired(state: ModelState, generation: int) -> bool:
    return generation > 0 and bool(state.retired_mask & (1 << (generation - 1)))


def _invalidate_nonterminal(state: ModelState) -> ModelState:
    if state.transaction_phase not in NONTERMINAL:
        return state
    return replace(
        state,
        transaction_phase="safe-failure",
        hardware_attempts=0,
    )


def _connect(state: ModelState, model: FormalModel) -> ModelState | None:
    if state.issued_generation >= model.receiver_generations:
        return None
    next_generation = state.issued_generation + 1
    retired_mask = state.retired_mask
    if state.active_generation:
        retired_mask |= 1 << (state.active_generation - 1)
    invalidated = _invalidate_nonterminal(state)
    return replace(
        invalidated,
        issued_generation=next_generation,
        active_generation=next_generation,
        retired_mask=retired_mask,
        qualified=False,
        lease_owner="none",
        lease_generation=0,
    )


def _disconnect(state: ModelState) -> ModelState | None:
    if state.active_generation == 0:
        return None
    invalidated = _invalidate_nonterminal(state)
    return replace(
        invalidated,
        active_generation=0,
        retired_mask=(
            invalidated.retired_mask | (1 << (state.active_generation - 1))
        ),
        qualified=False,
        lease_owner="none",
        lease_generation=0,
    )


def _successor_states(state: ModelState, model: FormalModel) -> Iterable[Transition]:
    connected = _connect(state, model)
    if connected is not None:
        yield "connect-generation", connected
    disconnected = _disconnect(state)
    if disconnected is not None:
        yield "disconnect-generation", disconnected
    if state.active_generation and not state.qualified:
        yield "qualify-generation", replace(state, qualified=True)
    if state.active_generation and state.qualified and state.lease_owner == "none":
        yield "acquire-application-lease", replace(
            state,
            lease_owner="application",
            lease_generation=state.active_generation,
        )
        yield "acquire-restoration-lease", replace(
            state,
            lease_owner="restoration",
            lease_generation=state.active_generation,
        )
    if state.lease_owner != "none" and state.transaction_phase in {"none", *TERMINAL}:
        yield "release-lease", replace(
            state,
            lease_owner="none",
            lease_generation=0,
        )
    if (
        state.active_generation
        and state.qualified
        and state.lease_owner != "none"
        and state.lease_generation == state.active_generation
        and state.transaction_phase == "none"
    ):
        yield "queue-transaction", replace(
            state,
            transaction_phase="queued",
            transaction_generation=state.active_generation,
            hardware_attempts=0,
        )
    if state.transaction_phase == "queued":
        yield "authorize-transaction", replace(state, transaction_phase="authorized")
    if state.transaction_phase == "authorized":
        yield "reserve-transport", replace(state, transaction_phase="reserved")
    if state.transaction_phase == "reserved":
        yield "dispatch-success", replace(
            state,
            transaction_phase="success",
            hardware_attempts=1,
        )
        yield "dispatch-safe-failure", replace(
            state,
            transaction_phase="safe-failure",
            hardware_attempts=0,
        )
        yield "dispatch-uncertain", replace(
            state,
            transaction_phase="uncertain",
            hardware_attempts=1,
        )
    if state.transaction_phase == "success" and (
        state.stable_intent_generation != state.transaction_generation
    ):
        yield "capture-stable-intent", replace(
            state,
            stable_intent_generation=state.transaction_generation,
        )
    if state.transaction_phase in {"success", "safe-failure"}:
        yield "clear-safe-terminal", replace(
            state,
            transaction_phase="none",
            transaction_generation=0,
            hardware_attempts=0,
        )


def _invariant_failures(state: ModelState) -> tuple[str, ...]:
    failures: list[str] = []
    if (
        state.active_generation < 0
        or state.active_generation > state.issued_generation
        or _retired(state, state.active_generation)
    ):
        failures.append("HFX-MODEL-001")
    if state.lease_owner not in OWNERS or (
        state.lease_owner == "none"
        and state.lease_generation != 0
    ) or (
        state.lease_owner != "none"
        and (
            state.lease_generation != state.active_generation
            or not state.qualified
            or state.active_generation == 0
        )
    ):
        failures.append("HFX-MODEL-002")
    if state.transaction_phase not in PHASES or (
        state.transaction_phase == "none" and state.transaction_generation != 0
    ) or (
        state.transaction_phase in NONTERMINAL
        and (
            state.transaction_generation != state.active_generation
            or state.lease_owner == "none"
            or state.lease_generation != state.active_generation
            or not state.qualified
        )
    ):
        failures.append("HFX-MODEL-003")
    if state.hardware_attempts not in {0, 1}:
        failures.append("HFX-MODEL-004")
    if (
        state.transaction_phase == "success"
        and state.hardware_attempts != 1
    ) or (
        state.transaction_phase == "safe-failure"
        and state.hardware_attempts != 0
    ):
        failures.append("HFX-MODEL-005")
    if state.transaction_phase == "uncertain" and state.hardware_attempts != 1:
        failures.append("HFX-MODEL-006")
    if state.active_generation and _retired(state, state.active_generation):
        failures.append("HFX-MODEL-007")
    if (
        state.stable_intent_generation < 0
        or state.stable_intent_generation > state.issued_generation
    ):
        failures.append("HFX-MODEL-008")
    return tuple(failures)


def run_formal_model(model: FormalModel) -> ModelResult:
    initial = ModelState()
    queue = deque([(initial, 0)])
    seen = {initial}
    observed_transitions: set[str] = set()
    transition_count = 0
    maximum_depth_reached = 0
    while queue:
        state, depth = queue.popleft()
        failures = _invariant_failures(state)
        if failures:
            raise ModelError(
                "formal model invariant failure "
                f"{', '.join(failures)} at depth {depth}: {state}"
            )
        maximum_depth_reached = max(maximum_depth_reached, depth)
        if depth == model.maximum_depth:
            continue
        for transition, successor in _successor_states(state, model):
            observed_transitions.add(transition)
            transition_count += 1
            if successor.stable_intent_generation != state.stable_intent_generation and not (
                transition == "capture-stable-intent"
                and state.transaction_phase == "success"
                and successor.stable_intent_generation == state.transaction_generation
            ):
                raise ModelError(
                    "formal model invariant failure after "
                    f"{transition}: HFX-MODEL-008: {successor}"
                )
            failures = _invariant_failures(successor)
            if failures:
                raise ModelError(
                    "formal model invariant failure after "
                    f"{transition}: {', '.join(failures)}: {successor}"
                )
            if successor in seen:
                continue
            seen.add(successor)
            if len(seen) > model.maximum_states:
                raise ModelError(
                    f"formal model exceeded its {model.maximum_states}-state bound"
                )
            queue.append((successor, depth + 1))
    missing = sorted(set(model.required_transitions) - observed_transitions)
    unknown = sorted(observed_transitions - set(model.required_transitions))
    if missing or unknown:
        raise ModelError(
            "formal model transition catalog differs from executable transitions: "
            f"missing={missing or 'none'} unknown={unknown or 'none'}"
        )
    return ModelResult(
        states=len(seen),
        transitions=transition_count,
        maximum_depth_reached=maximum_depth_reached,
        transition_names=tuple(sorted(observed_transitions)),
    )
