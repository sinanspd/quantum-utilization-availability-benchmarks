#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import cm, colors
import networkx as nx
import numpy as np
import pandas as pd


def read_csv_from_zip(zf: zipfile.ZipFile, member: str, **kwargs) -> pd.DataFrame:
    with zf.open(member) as f:
        return pd.read_csv(f, compression="gzip", **kwargs)


def parse_edge(edge_id: str) -> tuple[int, int]:
    a, b = edge_id.split("-")
    return int(a), int(b)


def compute_full_period_edge_ratios(component_cadence: pd.DataFrame) -> pd.DataFrame:
    edge = component_cadence[component_cadence["component_type"] == "edge"].copy()
    g = edge.groupby("backend")

    idx_max = g["calibration_event_count"].idxmax()
    idx_min = g["calibration_event_count"].idxmin()

    out = g["calibration_event_count"].agg(min_count="min", max_count="max").reset_index()
    out["full_period_ratio"] = out["max_count"] / out["min_count"]

    out = out.merge(
        edge.loc[idx_max, ["backend", "component_id", "calibration_event_count"]].rename(
            columns={
                "component_id": "peak_edge",
                "calibration_event_count": "peak_edge_count",
            }
        ),
        on="backend",
        how="left",
    )

    out = out.merge(
        edge.loc[idx_min, ["backend", "component_id", "calibration_event_count"]].rename(
            columns={
                "component_id": "min_edge",
                "calibration_event_count": "min_edge_count",
            }
        ),
        on="backend",
        how="left",
    )

    return out.sort_values("full_period_ratio", ascending=False)


def compute_weekly_edge_extremes(calibration_events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = calibration_events.copy()
    df = df[(df["in_study_period"] == True) & (df["component_type"] == "edge")].copy()

    for col in ["event_timestamp_utc", "study_start_utc", "study_end_utc"]:
        df[col] = pd.to_datetime(df[col], utc=True)

    elapsed_days = (df["event_timestamp_utc"] - df["study_start_utc"]).dt.total_seconds() / (24 * 3600)
    df["week_index"] = np.floor(elapsed_days / 7).astype(int)
    df["week_start_utc"] = df["study_start_utc"] + pd.to_timedelta(df["week_index"] * 7, unit="D")
    df["week_end_utc"] = df["week_start_utc"] + pd.Timedelta(days=7)

    # Keep only full 7-day bins completely contained in the study interval
    df = df[df["week_end_utc"] <= df["study_end_utc"]].copy()

    counts = (
        df.groupby(["backend", "week_index", "week_start_utc", "week_end_utc", "component_id"])
          .size()
          .reset_index(name="event_count")
    )

    rows = []
    for (backend, week_index, week_start, week_end), sub in counts.groupby(
        ["backend", "week_index", "week_start_utc", "week_end_utc"]
    ):
        positive = sub[sub["event_count"] > 0].copy()
        if positive.empty:
            continue

        max_row = positive.loc[positive["event_count"].idxmax()]
        min_row = positive.loc[positive["event_count"].idxmin()]

        rows.append(
            {
                "backend": backend,
                "week_index": int(week_index),
                "week_start_utc": week_start,
                "week_end_utc": week_end,
                "peak_events_week": int(max_row["event_count"]),
                "peak_edge": max_row["component_id"],
                "min_positive_events_week": int(min_row["event_count"]),
                "min_positive_edge": min_row["component_id"],
                "largest_weekly_ratio": float(max_row["event_count"]) / float(min_row["event_count"]),
                "active_edges_in_week": int(len(positive)),
                "total_edge_events_week": int(positive["event_count"].sum()),
            }
        )

    weekly_extremes = pd.DataFrame(rows)

    best_weekly = (
        weekly_extremes.sort_values(
            ["backend", "largest_weekly_ratio", "peak_events_week"],
            ascending=[True, False, False],
        )
        .groupby("backend", as_index=False)
        .first()
        .sort_values("largest_weekly_ratio", ascending=False)
    )

    return weekly_extremes, best_weekly


def compute_value_change_diagnostic(calibration_events: pd.DataFrame, best_weekly: pd.DataFrame) -> pd.DataFrame:
    df = calibration_events.copy()
    df = df[(df["in_study_period"] == True) & (df["component_type"] == "edge")].copy()

    out_rows = []
    for _, row in best_weekly.iterrows():
        backend = row["backend"]
        edge = row["peak_edge"]
        sub = df[(df["backend"] == backend) & (df["component_id"] == edge)].copy()

        ts_count = len(sub)
        changed = int((sub["drift_property_count"].fillna(0) > 0).sum())
        adverse = int((sub["degraded_property_count"].fillna(0) > 0).sum())

        out_rows.append(
            {
                "backend": backend,
                "extreme_edge": edge,
                "timestamp_update_events": ts_count,
                "events_with_nonzero_reported_value_change": changed,
                "events_with_any_adverse_change": adverse,
                "fraction_with_nonzero_reported_value_change": changed / ts_count if ts_count else np.nan,
                "fraction_with_any_adverse_change": adverse / ts_count if ts_count else np.nan,
                "peak_events_week": row["peak_events_week"],
                "largest_weekly_ratio": row["largest_weekly_ratio"],
            }
        )

    return pd.DataFrame(out_rows).sort_values("largest_weekly_ratio", ascending=False)


def compute_constant_one_gate_error_diagnostic(property_drift: pd.DataFrame) -> pd.DataFrame:
    df = property_drift.copy()
    df = df[df["component_type"] == "edge"].copy()
    df = df[df["property_name"].str.contains("gate_error", na=False)].copy()

    g = df.groupby(["backend", "component_id", "property_name"])
    out = g.agg(
        observations=("value", "size"),
        unique_values=("value", "nunique"),
        min_value=("value", "min"),
        max_value=("value", "max"),
    ).reset_index()

    out["all_values_equal_1_0"] = (out["min_value"] == 1.0) & (out["max_value"] == 1.0)
    return out.sort_values(["all_values_equal_1_0", "observations"], ascending=[False, False])


def compute_weekly_backend_edge_event_volume(calibration_events: pd.DataFrame) -> pd.DataFrame:
    df = calibration_events.copy()
    df = df[(df["in_study_period"] == True) & (df["component_type"] == "edge")].copy()

    for col in ["event_timestamp_utc", "study_start_utc", "study_end_utc"]:
        df[col] = pd.to_datetime(df[col], utc=True)

    elapsed_days = (df["event_timestamp_utc"] - df["study_start_utc"]).dt.total_seconds() / (24 * 3600)
    df["week_index"] = np.floor(elapsed_days / 7).astype(int)
    df["week_start_utc"] = df["study_start_utc"] + pd.to_timedelta(df["week_index"] * 7, unit="D")
    df["week_end_utc"] = df["week_start_utc"] + pd.Timedelta(days=7)

    df = df[df["week_end_utc"] <= df["study_end_utc"]].copy()

    weekly = (
        df.groupby(["backend", "week_index", "week_start_utc", "week_end_utc"])
          .size()
          .reset_index(name="total_edge_events_week")
          .sort_values(["backend", "week_index"])
    )

    return weekly


def choose_backend(full_period_ratios: pd.DataFrame, backend: str | None) -> str:
    if backend:
        return backend
    return full_period_ratios.sort_values("full_period_ratio", ascending=False).iloc[0]["backend"]


def plot_topology_heatmap(component_cadence: pd.DataFrame, backend: str, outpath: Path) -> None:
    edge = component_cadence[
        (component_cadence["component_type"] == "edge")
        & (component_cadence["backend"] == backend)
    ].copy()

    qubit = component_cadence[
        (component_cadence["component_type"] == "qubit")
        & (component_cadence["backend"] == backend)
    ].copy()

    G = nx.Graph()

    for _, r in qubit.iterrows():
        q = int(r["component_id"])
        G.add_node(q, node_freq=float(r["calibration_events_per_day"]))

    for _, r in edge.iterrows():
        u, v = parse_edge(r["component_id"])
        G.add_edge(
            u,
            v,
            edge_freq=float(r["calibration_events_per_day"]),
            edge_count=int(r["calibration_event_count"]),
        )

    # Graph layout. This is topology-faithful in connectivity,
    # but not IBM's exact chip-coordinate map.
    pos = nx.spring_layout(G, seed=7, k=0.22, iterations=300)

    node_freq = np.array([G.nodes[n].get("node_freq", np.nan) for n in G.nodes()])
    edge_freq = np.array([G.edges[e].get("edge_freq", np.nan) for e in G.edges()])

    fig, ax = plt.subplots(figsize=(12, 9))
    ax.set_title(
        f"{backend}: Edge Update Cadence Across the Coupling Graph"
    )

    if len(edge_freq) > 0:
        # Edge frequencies are strongly right-skewed.  A logarithmic
        # normalization prevents the majority of edges from collapsing
        # into the lowest color range.
        positive_edge_freq = edge_freq[edge_freq > 0]

        edge_norm = colors.LogNorm(
            vmin=float(np.nanmin(positive_edge_freq)),
            vmax=float(np.nanmax(positive_edge_freq)),
        )

        edge_colors = cm.viridis(edge_norm(edge_freq))

        nx.draw_networkx_edges(
            G,
            pos,
            ax=ax,
            edge_color=edge_colors,
            width=2.0,
            alpha=0.9,
        )

        edge_sm = cm.ScalarMappable(
            norm=edge_norm,
            cmap=cm.viridis,
        )
        edge_sm.set_array([])

        cbar1 = fig.colorbar(
            edge_sm,
            ax=ax,
            fraction=0.030,
            pad=0.02,
        )
        cbar1.set_label("Edge update events per day")

    # if len(node_freq) > 0:
    #     node_norm = colors.Normalize(vmin=float(np.nanmin(node_freq)), vmax=float(np.nanmax(node_freq)))
    #     node_colors = cm.plasma(node_norm(node_freq))
    #     nx.draw_networkx_nodes(G, pos, ax=ax, node_color=node_colors, node_size=45, linewidths=0.2)

    #     node_sm = cm.ScalarMappable(norm=node_norm, cmap=cm.plasma)
    #     node_sm.set_array([])
    #     cbar2 = fig.colorbar(node_sm, ax=ax, fraction=0.030, pad=0.04)
    #     cbar2.set_label("qubit events per day")
    # Qubit cadence is comparatively homogeneous, so display qubits
    # uniformly and reserve the heat scale for edge-frequency variation.
    nx.draw_networkx_nodes(
        G,
        pos,
        ax=ax,
        node_size=45,
        node_color="lightgray",
        edgecolors="black",
        linewidths=0.3,
    )

    # counts = edge["calibration_event_count"]
    # max_row = edge.loc[counts.idxmax()]
    # min_row = edge.loc[counts.idxmin()]
    # ratio = float(counts.max()) / float(counts.min())

    # ax.text(
    #     0.01,
    #     0.01,
    #     f"Edge full-period max/min count ratio = {ratio:.2f}\n"
    #     f"Peak edge = {max_row['component_id']} ({int(max_row['calibration_event_count'])} events); "
    #     f"min edge = {min_row['component_id']} ({int(min_row['calibration_event_count'])} events)",
    #     transform=ax.transAxes,
    #     fontsize=9,
    #     va="bottom",
    #     ha="left",
    #     bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.8),
    # )

    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(outpath, dpi=220, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate follow-up calibration-frequency diagnostics and a topology heat map."
    )
    parser.add_argument("--zip-path", default="/mnt/data/output(1).zip")
    parser.add_argument("--outdir", default="/mnt/data/followup_output")
    parser.add_argument(
        "--backend",
        default=None,
        help="Optional backend to plot. Defaults to the backend with the highest full-period edge ratio.",
    )
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(args.zip_path) as zf:
        component_cadence = read_csv_from_zip(zf, "output/analysis-rds/component_cadence.csv.gz")
        calibration_events = read_csv_from_zip(zf, "output/analysis-rds/calibration_events.csv.gz")
        property_drift = read_csv_from_zip(zf, "output/analysis-rds/property_drift.csv.gz")

    full_period_ratios = compute_full_period_edge_ratios(component_cadence)
    weekly_extremes_all, weekly_extremes_best = compute_weekly_edge_extremes(calibration_events)
    value_change_diagnostic = compute_value_change_diagnostic(calibration_events, weekly_extremes_best)
    constant_one_gate_error = compute_constant_one_gate_error_diagnostic(property_drift)
    weekly_backend_edge_event_volume = compute_weekly_backend_edge_event_volume(calibration_events)

    full_period_ratios.to_csv(outdir / "full_period_edge_ratios.csv", index=False)
    weekly_extremes_all.to_csv(outdir / "weekly_edge_extremes_all_weeks.csv", index=False)
    weekly_extremes_best.to_csv(outdir / "weekly_edge_extremes_best_per_backend.csv", index=False)
    value_change_diagnostic.to_csv(outdir / "extreme_edge_value_change_diagnostic.csv", index=False)
    constant_one_gate_error.to_csv(outdir / "constant_one_gate_error_diagnostic.csv", index=False)
    weekly_backend_edge_event_volume.to_csv(outdir / "weekly_backend_edge_event_volume.csv", index=False)

    chosen_backend = choose_backend(full_period_ratios, args.backend)
    heatmap_path = outdir / f"{chosen_backend}_topology_frequency_heatmap.png"
    plot_topology_heatmap(component_cadence, chosen_backend, heatmap_path)

    summary = {
        "chosen_backend_for_heatmap": chosen_backend,
        "full_period_edge_ratios_csv": str(outdir / "full_period_edge_ratios.csv"),
        "weekly_edge_extremes_csv": str(outdir / "weekly_edge_extremes_best_per_backend.csv"),
        "value_change_diagnostic_csv": str(outdir / "extreme_edge_value_change_diagnostic.csv"),
        "constant_one_gate_error_csv": str(outdir / "constant_one_gate_error_diagnostic.csv"),
        "weekly_backend_edge_event_volume_csv": str(outdir / "weekly_backend_edge_event_volume.csv"),
        "heatmap_png": str(heatmap_path),
    }

    (outdir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()