"""Unit tests for the bounded exact Weighted Max-Cut workload."""

from itertools import product
from typing import Any

import pytest
from computegrid_api.jobs.optimization import weighted_max_cut


@pytest.mark.parametrize(
    ("parameters", "optimal_weight"),
    [
        (
            {
                "nodes": ["a", "b", "c"],
                "edges": [
                    {"source": "a", "target": "b", "weight": 1},
                    {"source": "b", "target": "c", "weight": 1},
                    {"source": "a", "target": "c", "weight": 1},
                ],
            },
            2.0,
        ),
        (
            {
                "nodes": ["a", "b", "c", "d"],
                "edges": [
                    {"source": "a", "target": "b", "weight": 2},
                    {"source": "b", "target": "c", "weight": 3},
                    {"source": "c", "target": "d", "weight": 4},
                ],
            },
            9.0,
        ),
    ],
)
def test_exact_solver_returns_known_optimum(
    parameters: dict[str, Any], optimal_weight: float
) -> None:
    result = weighted_max_cut(parameters)

    assert result["cut_weight"] == optimal_weight
    assert result["solver"] == {
        "name": "exact_classical_enumeration",
        "optimal": True,
        "assignments_evaluated": 2 ** (len(parameters["nodes"]) - 1),
        "symmetry_breaking": "a fixed to side_a",
    }
    partition = result["partition"]
    assert set(partition["side_a"]) | set(partition["side_b"]) == set(parameters["nodes"])
    assert set(partition["side_a"]).isdisjoint(partition["side_b"])


def test_qubo_energy_is_negative_cut_weight_for_every_assignment() -> None:
    parameters: dict[str, Any] = {
        "nodes": ["a", "b", "c"],
        "edges": [
            {"source": "a", "target": "b", "weight": 2.5},
            {"source": "b", "target": "c", "weight": 1.5},
            {"source": "a", "target": "c", "weight": 4.0},
        ],
    }
    result = weighted_max_cut(parameters)
    qubo = result["qubo"]
    linear = {term["variable"]: term["bias"] for term in qubo["linear"]}
    quadratic = {
        (term["source"], term["target"]): term["bias"] for term in qubo["quadratic"]
    }

    for bits in product((0, 1), repeat=3):
        sample = dict(zip(parameters["nodes"], bits, strict=True))
        energy = sum(linear[node] * value for node, value in sample.items()) + sum(
            bias * sample[source] * sample[target]
            for (source, target), bias in quadratic.items()
        )
        cut_weight = sum(
            edge["weight"]
            for edge in parameters["edges"]
            if sample[edge["source"]] != sample[edge["target"]]
        )
        assert energy == pytest.approx(-cut_weight)
