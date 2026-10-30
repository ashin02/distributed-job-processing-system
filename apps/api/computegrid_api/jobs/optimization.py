"""Bounded classical optimization workloads and exact local solvers."""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose
from typing import Any

from computegrid_api.api.schemas import (
    BQMParameters,
    BQMVariableType,
    ConstraintSense,
    CQMParameters,
    ObjectiveSense,
    WeightedMaxCutParameters,
)


@dataclass(frozen=True, slots=True)
class BQMSolution:
    """Internal exact BQM result shared by generic BQM and Max-Cut jobs."""

    assignment: dict[str, int]
    energy: float
    assignments_evaluated: int


def _assignment_for_mask(model: BQMParameters, mask: int) -> dict[str, int]:
    if model.vartype == BQMVariableType.BINARY:
        return {
            variable: int(bool(mask & (1 << index)))
            for index, variable in enumerate(model.variables)
        }
    return {
        variable: 1 if mask & (1 << index) else -1
        for index, variable in enumerate(model.variables)
    }


def evaluate_bqm_energy(model: BQMParameters, assignment: dict[str, int]) -> float:
    """Evaluate one complete assignment against a validated BQM."""
    return (
        model.offset
        + sum(
            model.linear.get(variable, 0.0) * assignment[variable]
            for variable in model.variables
        )
        + sum(
            term.bias * assignment[term.source] * assignment[term.target]
            for term in model.quadratic
        )
    )


def _solve_bqm(
    model: BQMParameters,
    *,
    fixed: dict[str, int] | None = None,
) -> BQMSolution:
    fixed_values = fixed or {}
    domain = {0, 1} if model.vartype == BQMVariableType.BINARY else {-1, 1}
    if not set(fixed_values).issubset(model.variables):
        raise ValueError("fixed variables must reference the BQM")
    if any(value not in domain for value in fixed_values.values()):
        raise ValueError("fixed values must belong to the BQM variable domain")

    best_assignment: dict[str, int] | None = None
    best_energy = float("inf")
    assignments_evaluated = 0
    for mask in range(1 << len(model.variables)):
        assignment = _assignment_for_mask(model, mask)
        if any(assignment[variable] != value for variable, value in fixed_values.items()):
            continue
        assignments_evaluated += 1
        energy = evaluate_bqm_energy(model, assignment)
        if energy < best_energy:
            best_assignment = assignment
            best_energy = energy

    if best_assignment is None:
        raise RuntimeError("bounded BQM enumeration produced no assignments")
    return BQMSolution(best_assignment, best_energy, assignments_evaluated)


def solve_bqm(parameters: dict[str, Any]) -> dict[str, Any]:
    """Solve a validated BINARY QUBO or SPIN Ising model exactly."""
    model = BQMParameters.model_validate(parameters)
    solution = _solve_bqm(model)
    return {
        "assignment": solution.assignment,
        "minimum_energy": solution.energy,
        "vartype": model.vartype.value,
        "solver": {
            "name": "exact_classical_enumeration",
            "optimal": True,
            "assignments_evaluated": solution.assignments_evaluated,
        },
    }


def weighted_max_cut(parameters: dict[str, Any]) -> dict[str, Any]:
    """Convert Weighted Max-Cut to a QUBO and solve it through the BQM core."""
    graph = WeightedMaxCutParameters.model_validate(parameters)
    node_index = {node: index for index, node in enumerate(graph.nodes)}
    linear = {node: 0.0 for node in graph.nodes}
    quadratic: list[dict[str, str | float]] = []

    for edge in graph.edges:
        linear[edge.source] -= edge.weight
        linear[edge.target] -= edge.weight
        source, target = (
            (edge.source, edge.target)
            if node_index[edge.source] < node_index[edge.target]
            else (edge.target, edge.source)
        )
        quadratic.append({"source": source, "target": target, "bias": 2.0 * edge.weight})

    model = BQMParameters.model_validate(
        {
            "vartype": "BINARY",
            "variables": graph.nodes,
            "linear": linear,
            "quadratic": quadratic,
            "offset": 0,
        }
    )
    solution = _solve_bqm(model, fixed={graph.nodes[0]: 0})
    side_a = [node for node in graph.nodes if solution.assignment[node] == 0]
    side_b = [node for node in graph.nodes if solution.assignment[node] == 1]
    return {
        "partition": {"side_a": side_a, "side_b": side_b},
        "cut_weight": -solution.energy,
        "qubo": {
            "variables": graph.nodes,
            "linear": [
                {"variable": node, "bias": bias} for node, bias in linear.items()
            ],
            "quadratic": quadratic,
            "offset": 0.0,
            "objective": "minimize",
            "energy_relation": "energy = -cut_weight",
        },
        "solver": {
            "name": "exact_classical_enumeration",
            "optimal": True,
            "assignments_evaluated": solution.assignments_evaluated,
            "symmetry_breaking": f"{graph.nodes[0]} fixed to side_a",
        },
    }


def _linear_value(coefficients: dict[str, float], assignment: dict[str, int]) -> float:
    return sum(bias * assignment[variable] for variable, bias in coefficients.items())


def _constraint_satisfied(lhs: float, sense: ConstraintSense, rhs: float) -> bool:
    if sense == ConstraintSense.LESS_THAN_EQUAL:
        return lhs <= rhs or isclose(lhs, rhs, abs_tol=1e-9)
    if sense == ConstraintSense.GREATER_THAN_EQUAL:
        return lhs >= rhs or isclose(lhs, rhs, abs_tol=1e-9)
    return isclose(lhs, rhs, abs_tol=1e-9)


def solve_cqm(parameters: dict[str, Any]) -> dict[str, Any]:
    """Solve the restricted binary/linear CQM subset by feasible enumeration."""
    model = CQMParameters.model_validate(parameters)
    best_assignment: dict[str, int] | None = None
    best_objective: float | None = None
    feasible_assignments = 0
    assignments_evaluated = 1 << len(model.variables)

    for mask in range(assignments_evaluated):
        assignment = {
            variable: int(bool(mask & (1 << index)))
            for index, variable in enumerate(model.variables)
        }
        if not all(
            _constraint_satisfied(
                _linear_value(constraint.linear, assignment),
                constraint.sense,
                constraint.rhs,
            )
            for constraint in model.constraints
        ):
            continue
        feasible_assignments += 1
        objective = model.objective.offset + _linear_value(model.objective.linear, assignment)
        if best_objective is None:
            better = True
        elif model.objective.sense == ObjectiveSense.MINIMIZE:
            better = objective < best_objective
        else:
            better = objective > best_objective
        if better:
            best_assignment = assignment
            best_objective = objective

    solver = {
        "name": "exact_classical_enumeration",
        "assignments_evaluated": assignments_evaluated,
        "feasible_assignments": feasible_assignments,
        "enumeration_complete": True,
    }
    if best_assignment is None:
        return {
            "status": "INFEASIBLE",
            "assignment": None,
            "objective_value": None,
            "objective_sense": model.objective.sense.value,
            "feasible": False,
            "optimal": False,
            "constraint_evaluations": [],
            "solver": solver,
        }

    evaluations = [
        {
            "name": constraint.name,
            "lhs": _linear_value(constraint.linear, best_assignment),
            "sense": constraint.sense.value,
            "rhs": constraint.rhs,
            "satisfied": True,
        }
        for constraint in model.constraints
    ]
    return {
        "status": "OPTIMAL",
        "assignment": best_assignment,
        "objective_value": best_objective,
        "objective_sense": model.objective.sense.value,
        "feasible": True,
        "optimal": True,
        "constraint_evaluations": evaluations,
        "solver": solver,
    }
