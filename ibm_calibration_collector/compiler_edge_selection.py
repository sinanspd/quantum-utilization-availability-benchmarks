from __future__ import annotations

import argparse
import json
import math
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import networkx as nx
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from qiskit import QuantumCircuit
from qiskit.transpiler import generate_preset_pass_manager
from qiskit_ibm_runtime import QiskitRuntimeService


DEFAULT_BACKENDS = (
    "ibm_boston",
    "ibm_fez",
    "ibm_kingston",
    "ibm_marrakesh",
    "ibm_pittsburgh",
)

DEFAULT_OPTIMIZATION_LEVELS = (2,)
DEFAULT_SEEDS = (0, 1, 2, 3, 4)


@dataclass(frozen=True)
class CircuitSpec:
    family: str
    name: str
    circuit: QuantumCircuit



def parse_csv_strings(value: str) -> tuple[str, ...]:
    result = tuple(x.strip() for x in value.split(",") if x.strip())
    if not result:
        raise argparse.ArgumentTypeError("expected a non-empty comma-separated list")
    return result


def parse_csv_ints(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(x.strip()) for x in value.split(",") if x.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    if not result:
        raise argparse.ArgumentTypeError("expected a non-empty comma-separated list")
    return result


def read_gzip_csv_from_zip(zf: zipfile.ZipFile, member: str, **kwargs) -> pd.DataFrame:
    with zf.open(member) as f:
        return pd.read_csv(f, compression="gzip", low_memory=False, **kwargs)


def load_component_cadence(analysis_zip: Path) -> pd.DataFrame:
    with zipfile.ZipFile(analysis_zip) as zf:
        return read_gzip_csv_from_zip(
            zf,
            "output/analysis-rds/component_cadence.csv.gz",
        )


def load_or_build_constant_one_edges(
    analysis_zip: Path,
    followup_dir: Path | None,
) -> pd.DataFrame:

    if followup_dir is not None:
        diagnostic_path = followup_dir / "constant_one_gate_error_diagnostic.csv"
        if diagnostic_path.exists():
            diagnostic = pd.read_csv(diagnostic_path)
            return summarize_constant_one_edges(diagnostic)

    print(
        "constant_one_gate_error_diagnostic.csv not found; "
        "reconstructing it from property_drift.csv.gz ..."
    )

    with zipfile.ZipFile(analysis_zip) as zf:
        property_drift = read_gzip_csv_from_zip(
            zf,
            "output/analysis-rds/property_drift.csv.gz",
            usecols=[
                "backend",
                "component_type",
                "component_id",
                "property_name",
                "value",
            ],
        )

    property_drift = property_drift[
        (property_drift["component_type"] == "edge")
        & property_drift["property_name"].str.contains("gate_error", na=False)
    ].copy()

    diagnostic = (
        property_drift.groupby(
            ["backend", "component_id", "property_name"],
            as_index=False,
        )
        .agg(
            observations=("value", "size"),
            unique_values=("value", "nunique"),
            min_value=("value", "min"),
            max_value=("value", "max"),
        )
    )

    diagnostic["all_values_equal_1_0"] = (
        diagnostic["min_value"].eq(1.0)
        & diagnostic["max_value"].eq(1.0)
    )

    return summarize_constant_one_edges(diagnostic)


def summarize_constant_one_edges(diagnostic: pd.DataFrame) -> pd.DataFrame:
    diag = diagnostic.copy()

    if "all_values_equal_1_0" not in diag.columns:
        raise ValueError(
            "constant-one diagnostic must contain all_values_equal_1_0"
        )

    # CSV round trips may turn booleans into strings.
    if diag["all_values_equal_1_0"].dtype == object:
        diag["all_values_equal_1_0"] = (
            diag["all_values_equal_1_0"]
            .astype(str)
            .str.lower()
            .isin(["true", "1", "yes"])
        )

    out = (
        diag.groupby(["backend", "component_id"], as_index=False)
        .agg(
            constant_one_gate_error_edge=("all_values_equal_1_0", "max"),
            constant_one_gate_error_series_count=(
                "all_values_equal_1_0",
                "sum",
            ),
        )
    )

    return out


# ---------------------------------------------------------------------------
# Circuit suite
# ---------------------------------------------------------------------------

def ghz_circuit(n: int) -> QuantumCircuit:
    qc = QuantumCircuit(n, name=f"ghz_{n}")
    qc.h(0)
    for i in range(n - 1):
        qc.cx(i, i + 1)
    return qc


def qft_circuit(n: int) -> QuantumCircuit:
    qc = QuantumCircuit(n, name=f"qft_{n}")

    for target in range(n):
        qc.h(target)
        for control in range(target + 1, n):
            angle = math.pi / (2 ** (control - target))
            qc.cp(angle, control, target)

    for i in range(n // 2):
        qc.swap(i, n - i - 1)

    return qc


def bernstein_vazirani_circuit(total_qubits: int) -> QuantumCircuit:
    if total_qubits < 3:
        raise ValueError("Bernstein-Vazirani requires at least 3 total qubits")

    n_data = total_qubits - 1
    ancilla = total_qubits - 1

    qc = QuantumCircuit(total_qubits, name=f"bv_{total_qubits}")

    # Prepare |-> ancilla and |+> data register.
    qc.x(ancilla)
    qc.h(ancilla)
    for q in range(n_data):
        qc.h(q)

    # Alternating 1010... secret gives a star-shaped interaction graph.
    for q in range(n_data):
        if q % 2 == 0:
            qc.cx(q, ancilla)

    for q in range(n_data):
        qc.h(q)

    return qc


def qaoa_ring_circuit(n: int, p: int = 2) -> QuantumCircuit:
    """
    QAOA-style MaxCut ansatz on an n-cycle.
    """
    qc = QuantumCircuit(n, name=f"qaoa_ring_{n}_p{p}")
    for q in range(n):
        qc.h(q)

    gammas = np.linspace(0.35, 0.80, p)
    betas = np.linspace(0.20, 0.55, p)

    for gamma, beta in zip(gammas, betas):
        for q in range(n):
            qc.rzz(2.0 * float(gamma), q, (q + 1) % n)
        for q in range(n):
            qc.rx(2.0 * float(beta), q)

    return qc


def ising_trotter_circuit(n: int, layers: int = 4) -> QuantumCircuit:
    """
    Trotterized nearest-neighbor transverse-field Ising evolution.
    """
    qc = QuantumCircuit(n, name=f"ising_{n}_l{layers}")

    for q in range(n):
        qc.h(q)

    for layer in range(layers):
        theta_zz = 0.22 + 0.03 * layer
        theta_x = 0.31 + 0.02 * layer

        for q in range(n - 1):
            qc.rzz(theta_zz, q, q + 1)

        for q in range(n):
            qc.rx(theta_x, q)

    return qc


def random_brickwork_circuit(
    n: int,
    layers: int = 6,
    circuit_seed: int = 1729,
) -> QuantumCircuit:
    rng = np.random.default_rng(circuit_seed + n)
    qc = QuantumCircuit(n, name=f"random_brickwork_{n}_l{layers}")

    for layer in range(layers):
        for q in range(n):
            qc.ry(float(rng.uniform(-math.pi, math.pi)), q)
            qc.rz(float(rng.uniform(-math.pi, math.pi)), q)

        start = layer % 2
        for q in range(start, n - 1, 2):
            qc.cx(q, q + 1)

    return qc


def dense_entangling_circuit(
    n: int,
    layers: int = 2,
) -> QuantumCircuit:
    qc = QuantumCircuit(
        n,
        name=f"dense_{n}_l{layers}",
    )

    for q in range(n):
        qc.h(q)

    for layer in range(layers):
        # Dense all-to-all logical interaction.
        for q1 in range(n):
            for q2 in range(q1 + 1, n):
                qc.cz(q1, q2)

        # RX does not commute with CZ, preventing adjacent dense CZ
        # layers from cancelling during optimization.
        for q in range(n):
            angle = 0.23 + 0.017 * (layer + 1) * (q + 1)
            qc.rx(angle, q)

        for q in range(n):
            qc.rz(
                0.11 * (layer + 1) * (q + 1),
                q,
            )

    return qc


def build_circuit_suite() -> list[CircuitSpec]:
    suite: list[CircuitSpec] = []

    for n in (4, 8, 16, 24):
        qc = ghz_circuit(n)
        suite.append(CircuitSpec("GHZ", qc.name, qc))

    for n in (4, 8, 12):
        qc = qft_circuit(n)
        suite.append(CircuitSpec("QFT", qc.name, qc))

    for n in (5, 9, 17, 25):
        qc = bernstein_vazirani_circuit(n)
        suite.append(CircuitSpec("Bernstein-Vazirani", qc.name, qc))

    for n in (6, 12, 18, 24):
        qc = qaoa_ring_circuit(n, p=2)
        suite.append(CircuitSpec("QAOA-ring", qc.name, qc))

    for n in (8, 16, 24):
        qc = ising_trotter_circuit(n, layers=4)
        suite.append(CircuitSpec("Ising-Trotter", qc.name, qc))

    for n in (8, 16, 24):
        qc = random_brickwork_circuit(n, layers=6)
        suite.append(CircuitSpec("Random-brickwork", qc.name, qc))

    for n in (6, 10, 14):
        qc = dense_entangling_circuit(n, layers=2)
        suite.append(CircuitSpec("Dense-entangling", qc.name, qc))

    return suite


# ---------------------------------------------------------------------------
# Backend topology
# ---------------------------------------------------------------------------

def backend_undirected_edges(backend) -> list[tuple[int, int]]:
    coupling = getattr(backend, "coupling_map", None)

    if coupling is None:
        target = getattr(backend, "target", None)
        if target is None:
            raise RuntimeError(
                f"{backend.name}: no coupling_map or target available"
            )
        coupling = target.build_coupling_map()

    if coupling is None:
        raise RuntimeError(f"{backend.name}: backend has no coupling map")

    if hasattr(coupling, "get_edges"):
        directed_edges = coupling.get_edges()
    else:
        directed_edges = coupling

    return sorted(
        {
            tuple(sorted((int(a), int(b))))
            for a, b in directed_edges
            if int(a) != int(b)
        }
    )


def topology_metrics(backend_name: str, edges: Iterable[tuple[int, int]]) -> pd.DataFrame:
    graph = nx.Graph()
    graph.add_edges_from(edges)

    betweenness = nx.edge_betweenness_centrality(graph, normalized=True)
    degree = dict(graph.degree())

    rows = []
    for u, v in sorted(graph.edges()):
        key = tuple(sorted((u, v)))
        rows.append(
            {
                "backend": backend_name,
                "component_id": f"{key[0]}-{key[1]}",
                "edge_betweenness": float(betweenness.get(key, betweenness.get((v, u), 0.0))),
                "endpoint_degree_sum": int(degree[u] + degree[v]),
                "endpoint_degree_max": int(max(degree[u], degree[v])),
                "endpoint_degree_min": int(min(degree[u], degree[v])),
            }
        )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Compiler usage extraction
# ---------------------------------------------------------------------------

def logical_two_qubit_count(circuit: QuantumCircuit) -> int:
    return sum(
        1
        for instruction in circuit.data
        if len(instruction.qubits) == 2
    )


def compiled_edge_counts(circuit: QuantumCircuit) -> dict[tuple[int, int], int]:
    """
    Count all native two-qubit instructions in the compiled physical circuit.

    After transpilation to a backend, the circuit's qubit indices correspond
    to physical backend qubits, so find_bit(q).index gives the physical index.
    """
    counts: dict[tuple[int, int], int] = {}

    for instruction in circuit.data:
        qargs = instruction.qubits
        if len(qargs) != 2:
            continue

        q0 = int(circuit.find_bit(qargs[0]).index)
        q1 = int(circuit.find_bit(qargs[1]).index)

        edge = tuple(sorted((q0, q1)))
        counts[edge] = counts.get(edge, 0) + 1

    return counts


def run_compiler_experiment(
    service: QiskitRuntimeService,
    backend_names: tuple[str, ...],
    optimization_levels: tuple[int, ...],
    seeds: tuple[int, ...],
    suite: list[CircuitSpec],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    run_rows: list[dict] = []
    edge_rows: list[dict] = []
    topology_frames: list[pd.DataFrame] = []
    backend_failures: list[str] = []

    for backend_name in backend_names:
        print(f"\n=== Loading {backend_name} ===")

        try:
            backend = service.backend(backend_name)
        except Exception as exc:
            message = f"{backend_name}: unable to load backend: {exc}"
            print("WARNING:", message)
            backend_failures.append(message)
            continue

        try:
            physical_edges = backend_undirected_edges(backend)
        except Exception as exc:
            message = f"{backend_name}: unable to read coupling map: {exc}"
            print("WARNING:", message)
            backend_failures.append(message)
            continue

        topology_frames.append(
            topology_metrics(backend_name, physical_edges)
        )

        for optimization_level in optimization_levels:
            for seed in seeds:
                print(
                    f"{backend_name}: optimization_level={optimization_level}, "
                    f"seed={seed}"
                )

                # Build a separate pass manager for each seed. Reuse it for the
                # whole logical suite at that backend/level/seed.
                pass_manager = generate_preset_pass_manager(
                    optimization_level=optimization_level,
                    backend=backend,
                    seed_transpiler=seed,
                )

                for spec in suite:
                    logical = spec.circuit

                    if logical.num_qubits > backend.num_qubits:
                        continue

                    started = time.perf_counter()

                    try:
                        compiled = pass_manager.run(logical)
                    except Exception as exc:
                        print(
                            f"WARNING: compile failed: {backend_name}, "
                            f"{spec.name}, level={optimization_level}, seed={seed}: {exc}"
                        )
                        continue

                    compile_seconds = time.perf_counter() - started
                    counts = compiled_edge_counts(compiled)

                    total_2q = int(sum(counts.values()))
                    unique_edges = int(len(counts))

                    run_id = (
                        f"{backend_name}|L{optimization_level}|S{seed}|{spec.name}"
                    )

                    run_rows.append(
                        {
                            "run_id": run_id,
                            "backend": backend_name,
                            "optimization_level": optimization_level,
                            "seed_transpiler": seed,
                            "family": spec.family,
                            "circuit_name": spec.name,
                            "logical_qubits": logical.num_qubits,
                            "logical_depth": logical.depth(),
                            "logical_two_qubit_ops": logical_two_qubit_count(logical),
                            "compiled_qubits": compiled.num_qubits,
                            "compiled_depth": compiled.depth(),
                            "compiled_two_qubit_ops": total_2q,
                            "unique_physical_edges_used": unique_edges,
                            "compile_seconds": compile_seconds,
                        }
                    )

                    if total_2q == 0:
                        continue

                    for (u, v), count in counts.items():
                        edge_rows.append(
                            {
                                "run_id": run_id,
                                "backend": backend_name,
                                "optimization_level": optimization_level,
                                "seed_transpiler": seed,
                                "family": spec.family,
                                "circuit_name": spec.name,
                                "logical_qubits": logical.num_qubits,
                                "component_id": f"{u}-{v}",
                                "two_qubit_operation_count": int(count),
                                "within_run_operation_share": float(count / total_2q),
                            }
                        )

    runs = pd.DataFrame(run_rows)
    edge_usage_by_run = pd.DataFrame(edge_rows)
    topology = (
        pd.concat(topology_frames, ignore_index=True)
        if topology_frames
        else pd.DataFrame()
    )

    return runs, edge_usage_by_run, topology, backend_failures


def aggregate_edge_usage(
    runs: pd.DataFrame,
    edge_usage_by_run: pd.DataFrame,
    topology: pd.DataFrame,
    cadence: pd.DataFrame,
    constant_one_edges: pd.DataFrame,
) -> pd.DataFrame:
    if runs.empty:
        return pd.DataFrame()

    run_counts = (
        runs.groupby(
            ["backend", "optimization_level"],
            as_index=False,
        )
        .agg(total_compiler_runs=("run_id", "nunique"))
    )

    if edge_usage_by_run.empty:
        usage = pd.DataFrame(
            columns=[
                "backend",
                "optimization_level",
                "component_id",
                "two_qubit_operation_count",
                "compiler_run_usage_count",
                "mean_within_run_operation_share",
            ]
        )
    else:
        usage = (
            edge_usage_by_run.groupby(
                ["backend", "optimization_level", "component_id"],
                as_index=False,
            )
            .agg(
                two_qubit_operation_count=(
                    "two_qubit_operation_count",
                    "sum",
                ),
                compiler_run_usage_count=("run_id", "nunique"),
                mean_within_run_operation_share=(
                    "within_run_operation_share",
                    "mean",
                ),
            )
        )


    levels = runs[["backend", "optimization_level"]].drop_duplicates()

    full = topology.merge(
        levels,
        on="backend",
        how="inner",
    )

    full = full.merge(
        usage,
        on=["backend", "optimization_level", "component_id"],
        how="left",
    )

    full = full.merge(
        run_counts,
        on=["backend", "optimization_level"],
        how="left",
    )

    for col in [
        "two_qubit_operation_count",
        "compiler_run_usage_count",
        "mean_within_run_operation_share",
    ]:
        full[col] = full[col].fillna(0)

    full["compiler_run_usage_fraction"] = (
        full["compiler_run_usage_count"]
        / full["total_compiler_runs"].replace(0, np.nan)
    )

    operation_totals = (
        full.groupby(
            ["backend", "optimization_level"]
        )["two_qubit_operation_count"]
        .transform("sum")
    )

    full["global_two_qubit_operation_share"] = (
        full["two_qubit_operation_count"]
        / operation_totals.replace(0, np.nan)
    )

    edge_cadence = cadence[
        cadence["component_type"] == "edge"
    ][
        [
            "backend",
            "component_id",
            "calibration_event_count",
            "calibration_events_per_day",
            "interarrival_mean_hours",
            "interarrival_median_hours",
            "interarrival_cv",
        ]
    ].copy()

    full = full.merge(
        edge_cadence,
        on=["backend", "component_id"],
        how="left",
    )

    full = full.merge(
        constant_one_edges,
        on=["backend", "component_id"],
        how="left",
    )

    full["constant_one_gate_error_edge"] = (
        full["constant_one_gate_error_edge"]
        .fillna(False)
        .astype(bool)
    )

    full["constant_one_gate_error_series_count"] = (
        full["constant_one_gate_error_series_count"]
        .fillna(0)
        .astype(int)
    )

    return full.sort_values(
        ["backend", "optimization_level", "component_id"]
    )


def bh_adjust(p_values: pd.Series) -> pd.Series:
    """
    Benjamini-Hochberg FDR adjustment.
    """
    p = pd.to_numeric(p_values, errors="coerce").to_numpy(dtype=float)
    q = np.full_like(p, np.nan, dtype=float)

    valid_idx = np.where(np.isfinite(p))[0]
    if len(valid_idx) == 0:
        return pd.Series(q, index=p_values.index)

    valid_p = p[valid_idx]
    order = np.argsort(valid_p)
    ranked = valid_p[order]

    adjusted = ranked * len(ranked) / np.arange(1, len(ranked) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    adjusted = np.minimum(adjusted, 1.0)

    q[valid_idx[order]] = adjusted
    return pd.Series(q, index=p_values.index)


def compute_correlations(edge_table: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []

    predictors = (
        "mean_within_run_operation_share",
        "compiler_run_usage_fraction",
        "global_two_qubit_operation_share",
        "edge_betweenness",
        "endpoint_degree_sum",
    )

    for (backend, level), group in edge_table.groupby(
        ["backend", "optimization_level"]
    ):
        for scope_name, scope in [
            ("all_edges", group),
            (
                "exclude_constant_1_gate_error_edges",
                group[~group["constant_one_gate_error_edge"]],
            ),
        ]:
            for predictor in predictors:
                sub = scope[
                    [predictor, "calibration_events_per_day"]
                ].dropna()

                if len(sub) < 8:
                    continue

                if sub[predictor].nunique() < 2:
                    rho = np.nan
                    p = np.nan
                else:
                    result = spearmanr(
                        sub[predictor],
                        sub["calibration_events_per_day"],
                        nan_policy="omit",
                    )
                    rho = float(result.statistic)
                    p = float(result.pvalue)

                rows.append(
                    {
                        "backend": backend,
                        "optimization_level": level,
                        "scope": scope_name,
                        "predictor": predictor,
                        "outcome": "calibration_events_per_day",
                        "n_edges": len(sub),
                        "spearman_rho": rho,
                        "p_value": p,
                    }
                )

    out = pd.DataFrame(rows)

    if not out.empty:
        out["q_value_bh_global"] = bh_adjust(out["p_value"])

    return out


def standardized_ols(
    frame: pd.DataFrame,
    outcome: str,
    predictors: list[str],
) -> tuple[pd.DataFrame, float, int]:
    cols = [outcome] + predictors
    df = frame[cols].dropna().copy()

    if len(df) <= len(predictors) + 2:
        return pd.DataFrame(), np.nan, len(df)

    # Outcome: log transform first, then standardize.
    y_raw = np.log1p(df[outcome].to_numpy(dtype=float))
    y_std = np.std(y_raw, ddof=0)
    if y_std == 0:
        return pd.DataFrame(), np.nan, len(df)
    y = (y_raw - np.mean(y_raw)) / y_std

    x_columns = []
    valid_predictors = []

    for predictor in predictors:
        x = df[predictor].to_numpy(dtype=float)
        std = np.std(x, ddof=0)
        if std == 0:
            continue
        x_columns.append((x - np.mean(x)) / std)
        valid_predictors.append(predictor)

    if not x_columns:
        return pd.DataFrame(), np.nan, len(df)

    X = np.column_stack(
        [np.ones(len(df))] + x_columns
    )

    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    fitted = X @ beta

    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan

    rows = [
        {
            "term": "intercept",
            "standardized_beta": float(beta[0]),
        }
    ]

    for predictor, coefficient in zip(
        valid_predictors,
        beta[1:],
    ):
        rows.append(
            {
                "term": predictor,
                "standardized_beta": float(coefficient),
            }
        )

    return pd.DataFrame(rows), r2, len(df)


def compute_topology_control_regressions(
    edge_table: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict] = []

    predictors = [
        "mean_within_run_operation_share",
        "edge_betweenness",
        "endpoint_degree_sum",
    ]

    for (backend, level), group in edge_table.groupby(
        ["backend", "optimization_level"]
    ):
        for scope_name, scope in [
            ("all_edges", group),
            (
                "exclude_constant_1_gate_error_edges",
                group[~group["constant_one_gate_error_edge"]],
            ),
        ]:
            coef, r2, n = standardized_ols(
                scope,
                outcome="calibration_events_per_day",
                predictors=predictors,
            )

            for _, row in coef.iterrows():
                rows.append(
                    {
                        "backend": backend,
                        "optimization_level": level,
                        "scope": scope_name,
                        "outcome": "log1p_calibration_events_per_day_standardized",
                        "term": row["term"],
                        "standardized_beta": row["standardized_beta"],
                        "r_squared": r2,
                        "n_edges": n,
                    }
                )

    return pd.DataFrame(rows)


def build_backend_summary(
    edge_table: pd.DataFrame,
    correlations: pd.DataFrame,
    regressions: pd.DataFrame,
) -> pd.DataFrame:
    rows = []

    for (backend, level), group in edge_table.groupby(
        ["backend", "optimization_level"]
    ):
        non_constant = group[
            ~group["constant_one_gate_error_edge"]
        ]

        def rho_for(scope: str, predictor: str) -> float:
            match = correlations[
                (correlations["backend"] == backend)
                & (correlations["optimization_level"] == level)
                & (correlations["scope"] == scope)
                & (correlations["predictor"] == predictor)
            ]
            return (
                float(match.iloc[0]["spearman_rho"])
                if not match.empty
                else np.nan
            )

        def beta_for(scope: str, term: str) -> float:
            match = regressions[
                (regressions["backend"] == backend)
                & (regressions["optimization_level"] == level)
                & (regressions["scope"] == scope)
                & (regressions["term"] == term)
            ]
            return (
                float(match.iloc[0]["standardized_beta"])
                if not match.empty
                else np.nan
            )

        def r2_for(scope: str) -> float:
            match = regressions[
                (regressions["backend"] == backend)
                & (regressions["optimization_level"] == level)
                & (regressions["scope"] == scope)
            ]
            return (
                float(match.iloc[0]["r_squared"])
                if not match.empty
                else np.nan
            )

        rows.append(
            {
                "backend": backend,
                "optimization_level": level,
                "n_topology_edges": len(group),
                "n_constant_1_gate_error_edges": int(
                    group["constant_one_gate_error_edge"].sum()
                ),
                "compiler_used_edge_fraction": float(
                    (group["compiler_run_usage_count"] > 0).mean()
                ),
                "compiler_used_edge_fraction_filtered": float(
                    (non_constant["compiler_run_usage_count"] > 0).mean()
                )
                if len(non_constant)
                else np.nan,
                "rho_compiler_share_vs_update_all": rho_for(
                    "all_edges",
                    "mean_within_run_operation_share",
                ),
                "rho_compiler_share_vs_update_filtered": rho_for(
                    "exclude_constant_1_gate_error_edges",
                    "mean_within_run_operation_share",
                ),
                "rho_compiler_presence_vs_update_all": rho_for(
                    "all_edges",
                    "compiler_run_usage_fraction",
                ),
                "rho_compiler_presence_vs_update_filtered": rho_for(
                    "exclude_constant_1_gate_error_edges",
                    "compiler_run_usage_fraction",
                ),
                "topology_controlled_compiler_beta_all": beta_for(
                    "all_edges",
                    "mean_within_run_operation_share",
                ),
                "topology_controlled_compiler_beta_filtered": beta_for(
                    "exclude_constant_1_gate_error_edges",
                    "mean_within_run_operation_share",
                ),
                "topology_controlled_r2_all": r2_for("all_edges"),
                "topology_controlled_r2_filtered": r2_for(
                    "exclude_constant_1_gate_error_edges"
                ),
            }
        )

    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Test whether the IBM/Qiskit preset compiler preferentially "
            "uses physical edges with high reported update cadence."
        )
    )

    parser.add_argument(
        "--analysis-zip",
        required=True,
        type=Path,
        help="Original output ZIP containing output/analysis-rds/*.csv.gz",
    )

    parser.add_argument(
        "--followup-dir",
        type=Path,
        default=None,
        help=(
            "Optional previous followup_output directory. If it contains "
            "constant_one_gate_error_diagnostic.csv, that file is reused."
        ),
    )

    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("compiler_edge_selection_output"),
    )

    parser.add_argument(
        "--backends",
        type=parse_csv_strings,
        default=DEFAULT_BACKENDS,
        help=(
            "Comma-separated IBM backend names. Default: "
            + ",".join(DEFAULT_BACKENDS)
        ),
    )

    parser.add_argument(
        "--optimization-levels",
        type=parse_csv_ints,
        default=DEFAULT_OPTIMIZATION_LEVELS,
        help=(
            "Comma-separated Qiskit optimization levels. "
            "Default: 2 (the preset pass-manager default). "
            "For a sensitivity run use 2,3."
        ),
    )

    parser.add_argument(
        "--seeds",
        type=parse_csv_ints,
        default=DEFAULT_SEEDS,
        help="Comma-separated transpiler seeds. Default: 0,1,2,3,4",
    )

    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)

    if not args.analysis_zip.exists():
        raise FileNotFoundError(args.analysis_zip)

    print("Loading calibration/update cadence ...")
    cadence = load_component_cadence(args.analysis_zip)

    print("Loading constant-1.0 gate-error sensitivity flags ...")
    constant_one_edges = load_or_build_constant_one_edges(
        args.analysis_zip,
        args.followup_dir,
    )

    suite = build_circuit_suite()

    suite_rows = []
    for spec in suite:
        suite_rows.append(
            {
                "family": spec.family,
                "circuit_name": spec.name,
                "logical_qubits": spec.circuit.num_qubits,
                "logical_depth": spec.circuit.depth(),
                "logical_two_qubit_ops": logical_two_qubit_count(spec.circuit),
            }
        )

    circuit_suite = pd.DataFrame(suite_rows)
    circuit_suite.to_csv(
        args.outdir / "circuit_suite.csv",
        index=False,
    )

    print("\nCircuit suite:")
    print(circuit_suite.to_string(index=False))

    print("\nConnecting to IBM Quantum ...")
    service = QiskitRuntimeService()

    runs, edge_usage_by_run, topology, backend_failures = (
        run_compiler_experiment(
            service=service,
            backend_names=args.backends,
            optimization_levels=args.optimization_levels,
            seeds=args.seeds,
            suite=suite,
        )
    )

    runs.to_csv(
        args.outdir / "compiler_runs.csv",
        index=False,
    )

    edge_usage_by_run.to_csv(
        args.outdir / "compiler_edge_usage_by_run.csv",
        index=False,
    )

    topology.to_csv(
        args.outdir / "backend_topology_metrics.csv",
        index=False,
    )

    edge_table = aggregate_edge_usage(
        runs=runs,
        edge_usage_by_run=edge_usage_by_run,
        topology=topology,
        cadence=cadence,
        constant_one_edges=constant_one_edges,
    )

    edge_table.to_csv(
        args.outdir / "compiler_edge_selection_summary.csv",
        index=False,
    )

    correlations = compute_correlations(edge_table)
    correlations.to_csv(
        args.outdir / "compiler_update_correlations.csv",
        index=False,
    )

    regressions = compute_topology_control_regressions(edge_table)
    regressions.to_csv(
        args.outdir / "compiler_topology_control_regressions.csv",
        index=False,
    )

    backend_summary = build_backend_summary(
        edge_table,
        correlations,
        regressions,
    )

    backend_summary.to_csv(
        args.outdir / "paper_backend_summary.csv",
        index=False,
    )

    metadata = {
        "analysis_zip": str(args.analysis_zip),
        "followup_dir": (
            str(args.followup_dir)
            if args.followup_dir is not None
            else None
        ),
        "backends_requested": list(args.backends),
        "optimization_levels": list(args.optimization_levels),
        "seeds": list(args.seeds),
        "circuit_count": len(suite),
        "circuit_families": sorted(circuit_suite["family"].unique().tolist()),
        "compiler_run_count": int(len(runs)),
        "backend_failures": backend_failures,
        "interpretation": (
            "This is a transpilation-only proxy for compiler preference. "
            "It does not recover historical physical edge utilization by "
            "executed cloud workloads."
        ),
    }

    (args.outdir / "run_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n=== Paper-level backend summary ===")
    if backend_summary.empty:
        print("No backend results were produced.")
    else:
        print(backend_summary.to_string(index=False))

    print(f"\nWrote results to: {args.outdir.resolve()}")



if __name__ == "__main__":
    main()
