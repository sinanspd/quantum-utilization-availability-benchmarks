#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import psycopg
from psycopg import sql

ANCHOR_COLUMN_ALIASES = (
    "last_update_date",
    "last_updated",
    "last_update",
    "backend_last_update_date",
    "backend_last_updated",
    "calibration_date",
    "backend_calibration_date",
)
BACKEND_COLUMN_ALIASES = ("backend", "backend_name", "device", "device_name")
POLL_COLUMN_ALIASES = (
    "poll_timestamp_utc",
    "observed_at_utc",
    "snapshot_timestamp_utc",
    "timestamp_utc",
    "poll_started_at",
    "created_at",
    "updated_at",
)
JSON_TYPES = {"json", "jsonb"}
TOLERANCES_MINUTES = (1, 5, 15, 30, 60, 120, 240, 480, 720, 1440)


@dataclass(frozen=True)
class AnchorSource:
    schema: str
    table: str
    backend_column: str
    anchor_column: str | None
    poll_column: str
    json_column: str | None = None
    json_path: tuple[str, ...] | None = None

    @property
    def description(self) -> str:
        base = f"{self.schema}.{self.table}"
        if self.anchor_column:
            return f"{base}.{self.anchor_column}"
        return f"{base}.{self.json_column} -> {'/'.join(self.json_path or ())}"


def parse_utc(value: str | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def read_frame(connection: psycopg.Connection[Any], query: Any, params: Iterable[Any] = ()) -> pd.DataFrame:
    with connection.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        cols = [d.name for d in cur.description or ()]
    return pd.DataFrame(rows, columns=cols)


def schema_inventory(connection: psycopg.Connection[Any]) -> pd.DataFrame:
    return read_frame(
        connection,
        """
        SELECT table_schema, table_name, column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY table_schema, table_name, ordinal_position
        """,
    )


def choose_alias(columns: set[str], aliases: tuple[str, ...]) -> str | None:
    lower_to_actual = {c.lower(): c for c in columns}
    for alias in aliases:
        if alias in lower_to_actual:
            return lower_to_actual[alias]
    return None


def recursively_find_key(obj: Any, aliases: set[str], path: tuple[str, ...] = ()) -> tuple[str, ...] | None:
    if isinstance(obj, dict):
        for k in obj:
            if str(k).lower() in aliases:
                return path + (str(k),)
        for k, v in obj.items():
            found = recursively_find_key(v, aliases, path + (str(k),))
            if found:
                return found
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            found = recursively_find_key(v, aliases, path + (str(i),))
            if found:
                return found
    return None


def discover_anchor_source(
    connection: psycopg.Connection[Any],
    inventory: pd.DataFrame,
    *,
    explicit_table: str | None,
    explicit_column: str | None,
    explicit_backend_column: str | None,
    explicit_poll_column: str | None,
) -> AnchorSource:
    grouped = {
        (schema_name, table_name): frame.copy()
        for (schema_name, table_name), frame in inventory.groupby(["table_schema", "table_name"], sort=False)
    }

    def split_table(name: str) -> tuple[str, str]:
        if "." in name:
            return tuple(name.split(".", 1))  # type: ignore[return-value]
        if ("public", name) in grouped:
            return "public", name
        matches = [(s, t) for (s, t) in grouped if t == name]
        if len(matches) == 1:
            return matches[0]
        raise RuntimeError(f"Could not uniquely resolve --anchor-table {name!r}; matches={matches}")

    if explicit_table:
        schema_name, table_name = split_table(explicit_table)
        frame = grouped.get((schema_name, table_name))
        if frame is None:
            raise RuntimeError(f"Anchor table {schema_name}.{table_name} does not exist")
        columns = set(frame["column_name"].astype(str))
        backend_col = explicit_backend_column or choose_alias(columns, BACKEND_COLUMN_ALIASES)
        poll_col = explicit_poll_column or choose_alias(columns, POLL_COLUMN_ALIASES)
        anchor_col = explicit_column or choose_alias(columns, ANCHOR_COLUMN_ALIASES)
        if not backend_col or not poll_col or not anchor_col:
            raise RuntimeError(
                "Explicit anchor table could not be resolved. "
                f"backend={backend_col}, poll={poll_col}, anchor={anchor_col}, available={sorted(columns)}"
            )
        return AnchorSource(schema_name, table_name, backend_col, anchor_col, poll_col)

    candidates: list[tuple[int, AnchorSource]] = []
    for (schema_name, table_name), frame in grouped.items():
        columns = set(frame["column_name"].astype(str))
        backend_col = choose_alias(columns, BACKEND_COLUMN_ALIASES)
        poll_col = choose_alias(columns, POLL_COLUMN_ALIASES)
        anchor_col = choose_alias(columns, ANCHOR_COLUMN_ALIASES)
        if not (backend_col and poll_col and anchor_col):
            continue

        score = 0
        if anchor_col.lower() == "last_update_date":
            score += 100
        elif anchor_col.lower() in {"last_updated", "last_update"}:
            score += 80
        else:
            score += 60
        lname = table_name.lower()
        if "backend" in lname:
            score += 20
        if "propert" in lname or "calibr" in lname:
            score += 20
        if "snapshot" in lname:
            score += 10
        if schema_name == "public":
            score += 5
        candidates.append((score, AnchorSource(schema_name, table_name, backend_col, anchor_col, poll_col)))

    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1].description), reverse=True)
        print("Candidate scalar backend calibration-anchor sources:", file=sys.stderr)
        for score, src in candidates[:10]:
            print(f"  score={score:3d}  {src.description}", file=sys.stderr)
        chosen = candidates[0][1]
        print(f"Using: {chosen.description}", file=sys.stderr)
        return chosen

    # JSON/JSONB fallback.
    json_candidates: list[tuple[int, AnchorSource]] = []
    aliases = set(ANCHOR_COLUMN_ALIASES)
    for (schema_name, table_name), frame in grouped.items():
        columns = set(frame["column_name"].astype(str))
        backend_col = choose_alias(columns, BACKEND_COLUMN_ALIASES)
        poll_col = choose_alias(columns, POLL_COLUMN_ALIASES)
        if not (backend_col and poll_col):
            continue
        json_cols = frame.loc[
            frame["data_type"].astype(str).str.lower().isin(JSON_TYPES)
            | frame["udt_name"].astype(str).str.lower().isin(JSON_TYPES),
            "column_name",
        ].astype(str).tolist()
        for json_col in json_cols:
            q = sql.SQL("SELECT {j} FROM {s}.{t} WHERE {j} IS NOT NULL LIMIT 5").format(
                j=sql.Identifier(json_col), s=sql.Identifier(schema_name), t=sql.Identifier(table_name)
            )
            sample = read_frame(connection, q)
            found_path = None
            for val in sample.iloc[:, 0].tolist() if not sample.empty else []:
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        continue
                found_path = recursively_find_key(val, aliases)
                if found_path:
                    break
            if found_path:
                score = 40
                lname = table_name.lower()
                if "backend" in lname:
                    score += 20
                if "propert" in lname or "calibr" in lname:
                    score += 20
                json_candidates.append(
                    (score, AnchorSource(schema_name, table_name, backend_col, None, poll_col, json_col, found_path))
                )

    if json_candidates:
        json_candidates.sort(key=lambda x: (x[0], x[1].description), reverse=True)
        print("Candidate JSON backend calibration-anchor sources:", file=sys.stderr)
        for score, src in json_candidates[:10]:
            print(f"  score={score:3d}  {src.description}", file=sys.stderr)
        chosen = json_candidates[0][1]
        print(f"Using: {chosen.description}", file=sys.stderr)
        return chosen

    raise RuntimeError(
        "Could not automatically locate a backend-level last_update_date field. "
        "Inspect schema_inventory.csv and rerun with --anchor-table, --anchor-column, "
        "--anchor-backend-column, and --anchor-poll-column."
    )


def build_time_where(column: str, start: pd.Timestamp | None, end: pd.Timestamp | None,
                     backends: list[str] | None, backend_column: str) -> tuple[Any, list[Any]]:
    clauses: list[Any] = []
    params: list[Any] = []
    if start is not None:
        clauses.append(sql.SQL("{} >= %s").format(sql.Identifier(column)))
        params.append(start.to_pydatetime())
    if end is not None:
        clauses.append(sql.SQL("{} <= %s").format(sql.Identifier(column)))
        params.append(end.to_pydatetime())
    if backends:
        clauses.append(sql.SQL("{} = ANY(%s)").format(sql.Identifier(backend_column)))
        params.append(backends)
    if not clauses:
        return sql.SQL("TRUE"), params
    out = clauses[0]
    for clause in clauses[1:]:
        out = out + sql.SQL(" AND ") + clause
    return out, params


def load_anchor_observations(connection: psycopg.Connection[Any], source: AnchorSource,
                             *, poll_start: pd.Timestamp | None, poll_end: pd.Timestamp | None,
                             backends: list[str] | None) -> pd.DataFrame:
    where, params = build_time_where(source.poll_column, poll_start, poll_end, backends, source.backend_column)

    if source.anchor_column:
        q = sql.SQL(
            """
            SELECT {backend}::text AS backend,
                   {poll} AS poll_timestamp_utc,
                   {anchor} AS backend_last_update_date
            FROM {schema}.{table}
            WHERE {where} AND {anchor} IS NOT NULL
            ORDER BY {backend}, {poll}
            """
        ).format(
            backend=sql.Identifier(source.backend_column),
            poll=sql.Identifier(source.poll_column),
            anchor=sql.Identifier(source.anchor_column),
            schema=sql.Identifier(source.schema),
            table=sql.Identifier(source.table),
            where=where,
        )
        frame = read_frame(connection, q, params)
    else:
        q = sql.SQL(
            """
            SELECT {backend}::text AS backend,
                   {poll} AS poll_timestamp_utc,
                   ({json_col} #>> %s)::text AS backend_last_update_date
            FROM {schema}.{table}
            WHERE {where} AND ({json_col} #>> %s) IS NOT NULL
            ORDER BY {backend}, {poll}
            """
        ).format(
            backend=sql.Identifier(source.backend_column),
            poll=sql.Identifier(source.poll_column),
            json_col=sql.Identifier(source.json_column or ""),
            schema=sql.Identifier(source.schema),
            table=sql.Identifier(source.table),
            where=where,
        )
        json_path = list(source.json_path or ())
        # Placeholder order: JSON path in SELECT, WHERE time params, JSON path in final predicate.
        frame = read_frame(connection, q, [json_path, *params, json_path])

    if frame.empty:
        raise RuntimeError(f"Anchor source {source.description} returned no rows")

    frame["poll_timestamp_utc"] = pd.to_datetime(frame["poll_timestamp_utc"], utc=True, errors="coerce")
    frame["backend_last_update_date"] = pd.to_datetime(frame["backend_last_update_date"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["backend", "poll_timestamp_utc", "backend_last_update_date"])

    return (
        frame.groupby(["backend", "backend_last_update_date"], as_index=False)
        .agg(
            first_observed_at_utc=("poll_timestamp_utc", "min"),
            last_observed_at_utc=("poll_timestamp_utc", "max"),
            poll_observation_count=("poll_timestamp_utc", "size"),
        )
        .sort_values(["backend", "backend_last_update_date"])
        .reset_index(drop=True)
    )


def load_property_history(connection: psycopg.Connection[Any], *, poll_start: pd.Timestamp | None,
                          poll_end: pd.Timestamp | None, backends: list[str] | None) -> pd.DataFrame:
    def where_for() -> tuple[str, list[Any]]:
        clauses = ["property_date IS NOT NULL", "value IS NOT NULL"]
        params: list[Any] = []
        if poll_start is not None:
            clauses.append("poll_timestamp_utc >= %s")
            params.append(poll_start.to_pydatetime())
        if poll_end is not None:
            clauses.append("poll_timestamp_utc <= %s")
            params.append(poll_end.to_pydatetime())
        if backends:
            clauses.append("backend = ANY(%s)")
            params.append(backends)
        return " AND ".join(clauses), params

    q_where, q_params = where_for()
    g_where, g_params = where_for()

    qubit_q = f"""
        SELECT DISTINCT ON (backend, qubit, property_name, COALESCE(unit, ''), property_date)
            backend,
            'qubit'::text AS component_type,
            qubit::text AS component_id,
            'qubit_property'::text AS source_kind,
            NULL::text AS gate_name,
            NULL::text AS gate_direction,
            property_name::text AS parameter_name,
            property_name::text AS property_family,
            property_name || '[' || COALESCE(unit, '') || ']' AS property_key,
            COALESCE(unit, '')::text AS unit,
            property_date AS property_timestamp_utc,
            value::double precision AS value,
            poll_timestamp_utc AS observed_at_utc
        FROM qubit_property_snapshots
        WHERE {q_where}
        ORDER BY backend, qubit, property_name, COALESCE(unit, ''), property_date, poll_timestamp_utc DESC
    """

    oneq_q = f"""
        SELECT DISTINCT ON (backend, qubits_key, gate_name, parameter_name, COALESCE(unit, ''), property_date)
            backend,
            'qubit'::text AS component_type,
            qubits[1]::text AS component_id,
            'one_qubit_gate_property'::text AS source_kind,
            gate_name::text AS gate_name,
            qubits_key::text AS gate_direction,
            parameter_name::text AS parameter_name,
            gate_name || ':' || parameter_name AS property_family,
            gate_name || '@' || qubits_key || ':' || parameter_name || '[' || COALESCE(unit, '') || ']' AS property_key,
            COALESCE(unit, '')::text AS unit,
            property_date AS property_timestamp_utc,
            value::double precision AS value,
            poll_timestamp_utc AS observed_at_utc
        FROM gate_property_snapshots
        WHERE {g_where} AND cardinality(qubits) = 1
        ORDER BY backend, qubits_key, gate_name, parameter_name, COALESCE(unit, ''), property_date, poll_timestamp_utc DESC
    """

    edge_q = f"""
        SELECT DISTINCT ON (backend, edge_id, gate_name, qubits_key, parameter_name, COALESCE(unit, ''), property_date)
            backend,
            'edge'::text AS component_type,
            edge_id::text AS component_id,
            'two_qubit_gate_property'::text AS source_kind,
            gate_name::text AS gate_name,
            qubits_key::text AS gate_direction,
            parameter_name::text AS parameter_name,
            gate_name || ':' || parameter_name AS property_family,
            gate_name || '@' || qubits_key || ':' || parameter_name || '[' || COALESCE(unit, '') || ']' AS property_key,
            COALESCE(unit, '')::text AS unit,
            property_date AS property_timestamp_utc,
            value::double precision AS value,
            poll_timestamp_utc AS observed_at_utc
        FROM gate_property_snapshots
        WHERE {g_where} AND edge_id IS NOT NULL
        ORDER BY backend, edge_id, gate_name, qubits_key, parameter_name, COALESCE(unit, ''), property_date, poll_timestamp_utc DESC
    """

    history = pd.concat(
        [read_frame(connection, qubit_q, q_params),
         read_frame(connection, oneq_q, g_params),
         read_frame(connection, edge_q, g_params)],
        ignore_index=True,
    )
    if history.empty:
        raise RuntimeError("No property history rows were returned")

    history["property_timestamp_utc"] = pd.to_datetime(history["property_timestamp_utc"], utc=True, errors="coerce")
    history["observed_at_utc"] = pd.to_datetime(history["observed_at_utc"], utc=True, errors="coerce")
    history = history.dropna(subset=["backend", "component_type", "component_id", "property_timestamp_utc"])
    history["series_id"] = (
        history["backend"].astype(str) + "|" + history["component_type"].astype(str) + "|" +
        history["component_id"].astype(str) + "|" + history["property_key"].astype(str)
    )
    history = history.sort_values(["series_id", "property_timestamp_utc"]).reset_index(drop=True)
    history["previous_property_timestamp_utc"] = history.groupby("series_id")["property_timestamp_utc"].shift(1)
    history["previous_value"] = history.groupby("series_id")["value"].shift(1)
    history["has_prior_series_value"] = history["previous_property_timestamp_utc"].notna()
    history["reported_value_changed"] = (
        history["has_prior_series_value"]
        & history["previous_value"].notna()
        & history["value"].notna()
        & (history["value"] != history["previous_value"])
    )
    return history


def nearest_anchor_columns(updates: pd.DataFrame, anchors: pd.DataFrame, timestamp_col: str) -> pd.DataFrame:
    out_frames: list[pd.DataFrame] = []
    for backend, u in updates.groupby("backend", sort=False):
        u = u.sort_values(timestamp_col).copy()
        a = anchors.loc[anchors["backend"] == backend, ["backend_last_update_date"]].drop_duplicates()
        a = a.sort_values("backend_last_update_date")
        if a.empty:
            u["previous_backend_anchor_utc"] = pd.NaT
            u["next_backend_anchor_utc"] = pd.NaT
            u["nearest_backend_anchor_utc"] = pd.NaT
            u["minutes_after_previous_anchor"] = pd.NA
            u["minutes_before_next_anchor"] = pd.NA
            u["signed_minutes_from_nearest_anchor"] = pd.NA
            u["abs_minutes_from_nearest_anchor"] = pd.NA
            out_frames.append(u)
            continue

        prev = pd.merge_asof(
            u[[timestamp_col]],
            a.rename(columns={"backend_last_update_date": "previous_backend_anchor_utc"}),
            left_on=timestamp_col,
            right_on="previous_backend_anchor_utc",
            direction="backward",
        )
        nxt = pd.merge_asof(
            u[[timestamp_col]],
            a.rename(columns={"backend_last_update_date": "next_backend_anchor_utc"}),
            left_on=timestamp_col,
            right_on="next_backend_anchor_utc",
            direction="forward",
        )
        u["previous_backend_anchor_utc"] = prev["previous_backend_anchor_utc"].to_numpy()
        u["next_backend_anchor_utc"] = nxt["next_backend_anchor_utc"].to_numpy()
        u["minutes_after_previous_anchor"] = (u[timestamp_col] - u["previous_backend_anchor_utc"]).dt.total_seconds() / 60.0
        u["minutes_before_next_anchor"] = (u["next_backend_anchor_utc"] - u[timestamp_col]).dt.total_seconds() / 60.0
        prev_abs = u["minutes_after_previous_anchor"].abs()
        next_abs = u["minutes_before_next_anchor"].abs()
        choose_prev = prev_abs.notna() & (next_abs.isna() | (prev_abs <= next_abs))
        u["nearest_backend_anchor_utc"] = u["next_backend_anchor_utc"]
        u.loc[choose_prev, "nearest_backend_anchor_utc"] = u.loc[choose_prev, "previous_backend_anchor_utc"]
        u["signed_minutes_from_nearest_anchor"] = (u[timestamp_col] - u["nearest_backend_anchor_utc"]).dt.total_seconds() / 60.0
        u["abs_minutes_from_nearest_anchor"] = u["signed_minutes_from_nearest_anchor"].abs()
        out_frames.append(u)
    return pd.concat(out_frames, ignore_index=True)


def add_alignment_flags(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for m in TOLERANCES_MINUTES:
        out[f"abs_within_{m}m"] = out["abs_minutes_from_nearest_anchor"].le(m).fillna(False)
        out[f"before_next_within_{m}m"] = (
            out["minutes_before_next_anchor"].ge(0) & out["minutes_before_next_anchor"].le(m)
        ).fillna(False)
        out[f"after_previous_within_{m}m"] = (
            out["minutes_after_previous_anchor"].ge(0) & out["minutes_after_previous_anchor"].le(m)
        ).fillna(False)
    return out


def build_component_events(property_updates: pd.DataFrame, tolerance_minutes: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (backend, ctype, cid), g in property_updates.groupby(["backend", "component_type", "component_id"], sort=False):
        g = g.sort_values("property_timestamp_utc")
        current_anchor: pd.Timestamp | None = None
        bucket: list[pd.Series] = []

        def flush(items: list[pd.Series]) -> None:
            if not items:
                return
            sub = pd.DataFrame(items)
            rows.append({
                "backend": backend,
                "component_type": ctype,
                "component_id": cid,
                "event_timestamp_utc": sub["property_timestamp_utc"].min(),
                "event_end_timestamp_utc": sub["property_timestamp_utc"].max(),
                "property_update_count": len(sub),
                "distinct_property_family_count": sub["property_family"].nunique(),
                "reported_value_changed_count": int(sub["reported_value_changed"].sum()),
                "any_reported_value_changed": bool(sub["reported_value_changed"].any()),
                "property_families": ";".join(sorted(set(sub["property_family"].astype(str)))),
            })

        for _, r in g.iterrows():
            ts = r["property_timestamp_utc"]
            if current_anchor is None:
                current_anchor = ts
                bucket = [r]
            elif ts - current_anchor <= pd.Timedelta(minutes=tolerance_minutes):
                bucket.append(r)
            else:
                flush(bucket)
                current_anchor = ts
                bucket = [r]
        flush(bucket)
    return pd.DataFrame(rows)


def summarize_alignment(frame: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, g in frame.groupby(group_cols, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = dict(zip(group_cols, keys))
        row["n_updates"] = len(g)
        row["median_signed_minutes"] = g["signed_minutes_from_nearest_anchor"].median()
        row["median_abs_minutes"] = g["abs_minutes_from_nearest_anchor"].median()
        row["p90_abs_minutes"] = g["abs_minutes_from_nearest_anchor"].quantile(0.90)
        row["p95_abs_minutes"] = g["abs_minutes_from_nearest_anchor"].quantile(0.95)
        row["fraction_before_nearest_anchor"] = g["signed_minutes_from_nearest_anchor"].lt(0).mean()
        row["fraction_exact_anchor"] = g["signed_minutes_from_nearest_anchor"].eq(0).mean()
        row["fraction_after_nearest_anchor"] = g["signed_minutes_from_nearest_anchor"].gt(0).mean()
        for m in TOLERANCES_MINUTES:
            row[f"fraction_abs_within_{m}m"] = g[f"abs_within_{m}m"].mean()
            row[f"fraction_before_next_within_{m}m"] = g[f"before_next_within_{m}m"].mean()
            row[f"fraction_after_previous_within_{m}m"] = g[f"after_previous_within_{m}m"].mean()
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Align IBM property timestamp advances with backend last_update_date anchors")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--start", help="Inclusive study start, ISO-8601 UTC")
    parser.add_argument("--end", help="Inclusive study end, ISO-8601 UTC")
    parser.add_argument(
        "--backends",
        default="ibm_boston,ibm_fez,ibm_kingston,ibm_marrakesh,ibm_pittsburgh",
        help="Comma-separated list; pass an empty string for all backends",
    )
    parser.add_argument("--baseline-lookback-days", type=int, default=7)
    parser.add_argument("--anchor-buffer-hours", type=int, default=24)
    parser.add_argument("--event-tolerance-minutes", type=int, default=15)
    parser.add_argument("--output-dir", type=Path, default=Path("output/calibration_anchor_alignment"))
    parser.add_argument("--anchor-table")
    parser.add_argument("--anchor-column")
    parser.add_argument("--anchor-backend-column")
    parser.add_argument("--anchor-poll-column")
    args = parser.parse_args()

    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if start is not None and end is not None and end < start:
        parser.error("--end must be >= --start")
    backends = [x.strip() for x in args.backends.split(",") if x.strip()] or None
    args.output_dir.mkdir(parents=True, exist_ok=True)

    property_poll_start = start - pd.Timedelta(days=args.baseline_lookback_days) if start is not None else None
    property_poll_end = end
    anchor_poll_start = (
        start - pd.Timedelta(days=args.baseline_lookback_days) - pd.Timedelta(hours=args.anchor_buffer_hours)
        if start is not None else None
    )
    anchor_poll_end = end + pd.Timedelta(hours=args.anchor_buffer_hours) if end is not None else None

    with psycopg.connect(args.database_url) as conn:
        inventory = schema_inventory(conn)
        inventory.to_csv(args.output_dir / "schema_inventory.csv", index=False)
        source = discover_anchor_source(
            conn,
            inventory,
            explicit_table=args.anchor_table,
            explicit_column=args.anchor_column,
            explicit_backend_column=args.anchor_backend_column,
            explicit_poll_column=args.anchor_poll_column,
        )
        anchors = load_anchor_observations(
            conn, source, poll_start=anchor_poll_start, poll_end=anchor_poll_end, backends=backends
        )
        history = load_property_history(
            conn, poll_start=property_poll_start, poll_end=property_poll_end, backends=backends
        )

    # First loaded value per series is baseline, not an inferred advance.
    updates = history.loc[history["has_prior_series_value"]].copy()
    if start is not None:
        updates = updates.loc[updates["property_timestamp_utc"] >= start].copy()
    if end is not None:
        updates = updates.loc[updates["property_timestamp_utc"] <= end].copy()
    if updates.empty:
        raise RuntimeError("No property timestamp advances remain after study filtering")

    aligned_updates = add_alignment_flags(nearest_anchor_columns(updates, anchors, "property_timestamp_utc"))
    component_events = build_component_events(aligned_updates, args.event_tolerance_minutes)
    aligned_events = add_alignment_flags(nearest_anchor_columns(component_events, anchors, "event_timestamp_utc"))

    summary = summarize_alignment(aligned_updates, ["backend", "component_type"])
    family_summary = summarize_alignment(aligned_updates, ["backend", "component_type", "property_family"])
    event_summary = summarize_alignment(aligned_events, ["backend", "component_type"])

    anchors.to_csv(args.output_dir / "backend_calibration_anchors.csv", index=False)
    aligned_updates.to_csv(
        args.output_dir / "property_updates_with_anchor_distance.csv.gz",
        index=False,
        compression="gzip",
        date_format="%Y-%m-%dT%H:%M:%S.%fZ",
    )
    aligned_events.to_csv(
        args.output_dir / "component_events_with_anchor_distance.csv.gz",
        index=False,
        compression="gzip",
        date_format="%Y-%m-%dT%H:%M:%S.%fZ",
    )
    summary.to_csv(args.output_dir / "alignment_summary.csv", index=False)
    family_summary.to_csv(args.output_dir / "property_family_alignment_summary.csv", index=False)
    event_summary.to_csv(args.output_dir / "component_event_alignment_summary.csv", index=False)

    metadata = {
        "anchor_source": {
            "description": source.description,
            "schema": source.schema,
            "table": source.table,
            "backend_column": source.backend_column,
            "anchor_column": source.anchor_column,
            "poll_column": source.poll_column,
            "json_column": source.json_column,
            "json_path": list(source.json_path or ()),
        },
        "study_start_utc": start.isoformat() if start is not None else None,
        "study_end_utc": end.isoformat() if end is not None else None,
        "backends": backends,
        "baseline_lookback_days": args.baseline_lookback_days,
        "anchor_buffer_hours": args.anchor_buffer_hours,
        "event_tolerance_minutes": args.event_tolerance_minutes,
        "diagnostic_alignment_tolerances_minutes": list(TOLERANCES_MINUTES),
        "row_counts": {
            "distinct_backend_calibration_anchors": int(len(anchors)),
            "property_history_rows_loaded": int(len(history)),
            "in_study_property_timestamp_advances": int(len(aligned_updates)),
            "component_events": int(len(aligned_events)),
        },
        "interpretation": (
            "Alignment with backend last_update_date is evidence of temporal association with a "
            "provider-reported backend calibration anchor, not proof that a specific component "
            "property was physically recalibrated."
        ),
    }
    (args.output_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")

    print("\nDone")
    print(f"Anchor source: {source.description}")
    print(f"Distinct backend anchors: {len(anchors):,}")
    print(f"In-study property timestamp advances: {len(aligned_updates):,}")
    print(f"Grouped component events: {len(aligned_events):,}")
    print(f"Output directory: {args.output_dir.resolve()}")
    print("Zip that output directory and upload it for analysis.")


if __name__ == "__main__":
    main()
