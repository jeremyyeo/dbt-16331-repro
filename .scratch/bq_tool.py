#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "google-cloud-bigquery>=3.11",
#     "pyyaml>=6.0",
# ]
# ///
"""
BigQuery helper that reuses the same connection credentials dbt uses (profiles.yml).

Usage:
  uv run .scratch/bq_tool.py test-connection
  uv run .scratch/bq_tool.py create-tables --prefix zz_sample_ --count 5
  uv run .scratch/bq_tool.py drop-tables --prefix zz_sample_
  uv run .scratch/bq_tool.py create-tables --prefix zz_sample_ --count 5 --dry-run
  uv run .scratch/bq_tool.py drop-tables --prefix zz_sample_ --dry-run
  uv run .scratch/bq_tool.py count-tables
  uv run .scratch/bq_tool.py count-tables --prefix zz_sample_
  uv run .scratch/bq_tool.py loop --prefix zz_sample_ --count 5
"""

import argparse
import sys
import time
from pathlib import Path

import yaml
from google.cloud.bigquery import Client, QueryJobConfig
from google.oauth2 import service_account

REPO_ROOT = Path(__file__).resolve().parent.parent

SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
]


def load_bq_target(
    profiles_dir: Path, profile_name: str | None, target_name: str | None
) -> dict:
    project_yml = yaml.safe_load((REPO_ROOT / "dbt_project.yml").read_text())
    profile_name = profile_name or project_yml["profile"]

    profiles_path = profiles_dir / "profiles.yml"
    profiles = yaml.safe_load(profiles_path.read_text())

    profile = profiles[profile_name]
    target_name = target_name or profile["target"]
    output = profile["outputs"][target_name]

    if output.get("type") != "bigquery":
        raise ValueError(
            f"Profile '{profile_name}' target '{target_name}' is not a bigquery target."
        )

    return output


def build_client(bq_config: dict) -> Client:
    keyfile = bq_config["keyfile"]
    keyfile_path = Path(keyfile)
    if not keyfile_path.is_absolute():
        keyfile_path = REPO_ROOT / keyfile_path

    credentials = service_account.Credentials.from_service_account_file(
        filename=str(keyfile_path),
        scopes=SCOPES,
    )
    return Client(credentials=credentials, project=bq_config["project"])


def run_query(client: Client, sql: str, bq_config: dict, dry_run: bool = False):
    # job_timeout_ms bounds how long BigQuery lets the job run server-side
    # (same knob dbt uses via job_execution_timeout_seconds). The client-side
    # result() wait is kept a bit longer than that so we get BigQuery's own
    # deadline-exceeded error back instead of a client-side
    # concurrent.futures.TimeoutError, whose str() is empty and prints as a
    # blank "Error: ".
    job_timeout_seconds = bq_config.get("job_execution_timeout_seconds", 300)

    job_config = QueryJobConfig(
        use_legacy_sql=False, dry_run=dry_run, priority="INTERACTIVE"
    )
    job_config.job_timeout_ms = job_timeout_seconds * 1000

    query_job = client.query(query=sql, job_config=job_config)
    if dry_run:
        return query_job
    return query_job.result(timeout=job_timeout_seconds + 30)


def cmd_test_connection(_args, bq_config: dict):
    client = build_client(bq_config)
    project = bq_config["project"]
    dataset = bq_config["dataset"]

    print(f"Connecting as project={project} dataset={dataset} ...")
    result = run_query(client, "select 1 as ok", bq_config)
    row = next(iter(result))
    assert row["ok"] == 1
    print("Connection OK.")

    dataset_ref = client.dataset(dataset, project=project)
    ds = client.get_dataset(dataset_ref)
    print(f"Dataset '{dataset}' exists in location '{ds.location}'.")


def create_sample_tables(
    client: Client,
    project: str,
    dataset: str,
    prefix: str,
    count: int,
    bq_config: dict,
    dry_run: bool = False,
):
    table_names = [f"{prefix}{i}" for i in range(1, count + 1)]

    # One multi-statement script submitted as a single query job, instead of
    # N separate query jobs.
    script = "\n".join(
        f"create or replace table `{project}`.`{dataset}`.`{table_name}` as (select {i} as id);"
        for i, table_name in enumerate(table_names, start=1)
    )
    job = run_query(client, script, bq_config, dry_run=dry_run)
    return table_names, job


def drop_sample_tables(
    client: Client, project: str, dataset: str, prefix: str, dry_run: bool = False
):
    dataset_ref = client.dataset(dataset, project=project)
    matches = [
        t.table_id
        for t in client.list_tables(dataset_ref)
        if t.table_id.startswith(prefix)
    ]

    if matches and not dry_run:
        for table_name in matches:
            client.delete_table(f"{project}.{dataset}.{table_name}", not_found_ok=True)

    return matches


def cmd_create_tables(args, bq_config: dict):
    client = build_client(bq_config)
    project = bq_config["project"]
    dataset = args.dataset or bq_config["dataset"]

    table_names, job = create_sample_tables(
        client, project, dataset, args.prefix, args.count, bq_config, args.dry_run
    )

    if args.dry_run:
        print(f"[dry-run] Would create {args.count} table(s):")
        for table_name in table_names:
            print(f"  `{project}.{dataset}.{table_name}`")
        print(f"[dry-run] Estimated bytes processed: {job.total_bytes_processed}")
        return

    print(
        f"Created {args.count} table(s) with prefix '{args.prefix}' in dataset '{dataset}':"
    )
    for table_name in table_names:
        print(f"  `{project}.{dataset}.{table_name}`")


def cmd_count_tables(args, bq_config: dict):
    client = build_client(bq_config)
    project = bq_config["project"]
    dataset = args.dataset or bq_config["dataset"]

    dataset_ref = client.dataset(dataset, project=project)
    table_ids = [t.table_id for t in client.list_tables(dataset_ref)]

    if args.prefix:
        table_ids = [t for t in table_ids if t.startswith(args.prefix)]
        print(
            f"{len(table_ids)} table(s) with prefix '{args.prefix}' in dataset '{dataset}'."
        )
    else:
        print(f"{len(table_ids)} table(s) in dataset '{dataset}'.")


def cmd_drop_tables(args, bq_config: dict):
    client = build_client(bq_config)
    project = bq_config["project"]
    dataset = args.dataset or bq_config["dataset"]

    dataset_ref = client.dataset(dataset, project=project)
    matches = [
        t.table_id
        for t in client.list_tables(dataset_ref)
        if t.table_id.startswith(args.prefix)
    ]

    if not matches:
        print(f"No tables with prefix '{args.prefix}' found in dataset '{dataset}'.")
        return

    if args.dry_run:
        print(f"[dry-run] Would drop {len(matches)} table(s):")
        for table_name in matches:
            print(f"  `{project}.{dataset}.{table_name}`")
        return

    for table_name in matches:
        table_ref = f"{project}.{dataset}.{table_name}"
        client.delete_table(table_ref, not_found_ok=True)
        print(f"Dropped `{table_ref}`.")

    print(
        f"Dropped {len(matches)} table(s) with prefix '{args.prefix}' in dataset '{dataset}'."
    )


def cmd_loop(args, bq_config: dict):
    client = build_client(bq_config)
    project = bq_config["project"]
    dataset = args.dataset or bq_config["dataset"]

    print(
        f"Looping create/drop of {args.count} table(s) with prefix '{args.prefix}' "
        f"in dataset '{dataset}'. Press Ctrl+C to stop."
    )

    iteration = 0
    try:
        while True:
            iteration += 1
            table_names, _ = create_sample_tables(
                client, project, dataset, args.prefix, args.count, bq_config
            )
            print(f"[iteration {iteration}] Created {len(table_names)} table(s).")

            dropped = drop_sample_tables(client, project, dataset, args.prefix)
            print(f"[iteration {iteration}] Dropped {len(dropped)} table(s).")

            if args.sleep:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        print(f"\nStopped after {iteration} iteration(s).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles-dir",
        default=str(REPO_ROOT),
        help="Directory containing profiles.yml (default: repo root, matching this project's setup).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="dbt profile name (default: from dbt_project.yml).",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="dbt target name (default: the profile's default target).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "test-connection", help="Test the BigQuery connection using dbt's credentials."
    )

    p_create = subparsers.add_parser(
        "create-tables", help="Create N sample tables with a prefix."
    )
    p_create.add_argument(
        "--prefix", required=True, help="Table name prefix, e.g. 'zz_sample_'."
    )
    p_create.add_argument(
        "--count", type=int, required=True, help="Number of sample tables to create."
    )
    p_create.add_argument(
        "--dataset",
        default=None,
        help="Override dataset (default: dataset from profiles.yml).",
    )
    p_create.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and estimate cost without actually creating tables.",
    )

    p_count = subparsers.add_parser(
        "count-tables", help="Count tables in a dataset, optionally filtered by prefix."
    )
    p_count.add_argument(
        "--prefix",
        default=None,
        help="Only count tables whose name starts with this prefix.",
    )
    p_count.add_argument(
        "--dataset",
        default=None,
        help="Override dataset (default: dataset from profiles.yml).",
    )

    p_drop = subparsers.add_parser("drop-tables", help="Drop all tables with a prefix.")
    p_drop.add_argument(
        "--prefix", required=True, help="Table name prefix to match for dropping."
    )
    p_drop.add_argument(
        "--dataset",
        default=None,
        help="Override dataset (default: dataset from profiles.yml).",
    )
    p_drop.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate without actually dropping tables.",
    )

    p_loop = subparsers.add_parser(
        "loop",
        help="Repeatedly create then drop N sample tables until interrupted (Ctrl+C).",
    )
    p_loop.add_argument(
        "--prefix", required=True, help="Table name prefix, e.g. 'zz_sample_'."
    )
    p_loop.add_argument(
        "--count",
        type=int,
        required=True,
        help="Number of sample tables to create/drop per iteration.",
    )
    p_loop.add_argument(
        "--dataset",
        default=None,
        help="Override dataset (default: dataset from profiles.yml).",
    )
    p_loop.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between iterations (default: 0).",
    )

    args = parser.parse_args()

    bq_config = load_bq_target(Path(args.profiles_dir), args.profile, args.target)

    if args.command == "test-connection":
        cmd_test_connection(args, bq_config)
    elif args.command == "create-tables":
        cmd_create_tables(args, bq_config)
    elif args.command == "count-tables":
        cmd_count_tables(args, bq_config)
    elif args.command == "drop-tables":
        cmd_drop_tables(args, bq_config)
    elif args.command == "loop":
        cmd_loop(args, bq_config)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
