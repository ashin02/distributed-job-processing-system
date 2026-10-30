"""Unit tests for the bounded exact Weighted Max-Cut workload."""

from itertools import product
from typing import Any

import pytest
from computegrid_api.jobs.optimization import solve_bqm, solve_cqm, weighted_max_cut


def test_binary_bqm_returns_minimum_energy_assignment() -> None:
    result = solve_bqm(
        {
            "vartype": "BINARY",
            "variables": ["x", "y"],
            "linear": {"x": -1, "y": -1},
            "quadratic": [{"source": "x", "target": "y", "bias": 2}],
            "offset": 0.5,
        }
    )

    assert result["assignment"] == {"x": 1, "y": 0}
    assert result["minimum_energy"] == -0.5
    assert result["vartype"] == "BINARY"
    assert result["solver"]["assignments_evaluated"] == 4


def test_spin_bqm_uses_minus_one_plus_one_domain() -> None:
    result = solve_bqm(
        {
            "vartype": "SPIN",
            "variables": ["s0", "s1"],
            "linear": {},
            "quadratic": [{"source": "s0", "target": "s1", "bias": -2}],
            "offset": 1,
        }
    )

    assert result["assignment"] == {"s0": -1, "s1": -1}
    assert result["minimum_energy"] == -1
    assert set(result["assignment"].values()) == {-1}
    assert result["vartype"] == "SPIN"


@pytest.mark.parametrize(
    ("sense", "expected_assignment", "expected_objective"),
    [
        ("maximize", {"x": 1, "y": 0}, 3.0),
        ("minimize", {"x": 0, "y": 0}, 0.0),
    ],
)
def test_cqm_optimizes_feasible_binary_assignments(
    sense: str,
    expected_assignment: dict[str, int],
    expected_objective: float,
) -> None:
    result = solve_cqm(
        {
            "variables": ["x", "y"],
            "objective": {"sense": sense, "linear": {"x": 3, "y": 2}, "offset": 0},
            "constraints": [
                {"name": "capacity", "linear": {"x": 1, "y": 1}, "sense": "<=", "rhs": 1},
                {"name": "nonnegative", "linear": {"x": 1}, "sense": ">=", "rhs": 0},
            ],
        }
    )

    assert result["status"] == "OPTIMAL"
    assert result["assignment"] == expected_assignment
    assert result["objective_value"] == expected_objective
    assert result["feasible"] is True
    assert result["optimal"] is True
    assert all(item["satisfied"] for item in result["constraint_evaluations"])


def test_cqm_equality_constraint_and_infeasible_result() -> None:
    equality = solve_cqm(
        {
            "variables": ["x", "y"],
            "objective": {"sense": "minimize", "linear": {"x": 2, "y": 1}},
            "constraints": [
                {"name": "choose-one", "linear": {"x": 1, "y": 1}, "sense": "==", "rhs": 1}
            ],
        }
    )
    infeasible = solve_cqm(
        {
            "variables": ["x"],
            "objective": {"sense": "minimize", "linear": {"x": 1}},
            "constraints": [
                {"name": "must-be-zero", "linear": {"x": 1}, "sense": "<=", "rhs": 0},
                {"name": "must-be-one", "linear": {"x": 1}, "sense": ">=", "rhs": 1},
            ],
        }
    )

    assert equality["assignment"] == {"x": 0, "y": 1}
    assert equality["objective_value"] == 1
    assert infeasible["status"] == "INFEASIBLE"
    assert infeasible["assignment"] is None
    assert infeasible["objective_value"] is None
    assert infeasible["feasible"] is False
    assert infeasible["optimal"] is False
    assert infeasible["solver"]["enumeration_complete"] is True


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
