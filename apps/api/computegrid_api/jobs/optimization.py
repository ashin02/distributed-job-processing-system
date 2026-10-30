"""Bounded classical optimization workloads and their QUBO models."""

from __future__ import annotations

from typing import Any

from computegrid_api.api.schemas import WeightedMaxCutParameters


def weighted_max_cut(parameters: dict[str, Any]) -> dict[str, Any]:
    """Solve a validated weighted Max-Cut graph by exact classical enumeration.

    The returned upper-triangular QUBO minimizes ``-cut_weight``. Fixing the
    first binary variable to zero removes the equivalent complemented cuts.
    """
    graph = WeightedMaxCutParameters.model_validate(parameters)
    node_index = {node: index for index, node in enumerate(graph.nodes)}
    linear = {node: 0.0 for node in graph.nodes}
    quadratic: dict[tuple[str, str], float] = {}

    for edge in graph.edges:
        linear[edge.source] -= edge.weight
        linear[edge.target] -= edge.weight
        source_index = node_index[edge.source]
        target_index = node_index[edge.target]
        key = (
            (edge.source, edge.target)
            if source_index < target_index
            else (edge.target, edge.source)
        )
        quadratic[key] = 2.0 * edge.weight

    best_weight = -1.0
    best_mask = 0
    assignments_evaluated = 1 << (len(graph.nodes) - 1)
    for reduced_mask in range(assignments_evaluated):
        mask = reduced_mask << 1
        cut_weight = sum(
            edge.weight
            for edge in graph.edges
            if bool(mask & (1 << node_index[edge.source]))
            != bool(mask & (1 << node_index[edge.target]))
        )
        if cut_weight > best_weight:
            best_weight = cut_weight
            best_mask = mask

    side_a = [
        node for index, node in enumerate(graph.nodes) if not best_mask & (1 << index)
    ]
    side_b = [node for index, node in enumerate(graph.nodes) if best_mask & (1 << index)]
    return {
        "partition": {"side_a": side_a, "side_b": side_b},
        "cut_weight": best_weight,
        "qubo": {
            "variables": graph.nodes,
            "linear": [
                {"variable": node, "bias": bias} for node, bias in linear.items()
            ],
            "quadratic": [
                {"source": source, "target": target, "bias": bias}
                for (source, target), bias in quadratic.items()
            ],
            "offset": 0.0,
            "objective": "minimize",
            "energy_relation": "energy = -cut_weight",
        },
        "solver": {
            "name": "exact_classical_enumeration",
            "optimal": True,
            "assignments_evaluated": assignments_evaluated,
            "symmetry_breaking": f"{graph.nodes[0]} fixed to side_a",
        },
    }
