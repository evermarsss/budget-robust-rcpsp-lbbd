# -*- coding: utf-8 -*-
"""
Sequential runner for the direct LG-baseline with terminal UI and Excel report.

Run:
    python run_lg.py

This script runs one Gurobi model at a time. It is intended for formal, fair
computational experiments. Do not run games/heavy programs in parallel if you
need publication-quality runtimes.
"""
from __future__ import annotations

import os
import time
import traceback
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

import gurobipy as gp
from gurobipy import GRB

import direct_lg_baseline as core
from benchmark_ui_common import ExcelReport, RunInfo, create_dashboard, now_str, to_num

# =============================================================================
# USER CONFIG
# =============================================================================
DATASET_ROOTS = {
    "J60": r"C:\Users\17717\Downloads\j60.sm",
}
DATASETS_TO_RUN = ["J60"]
OUTPUT_DIR = r"C:\Users\17717\Documents\GitHub\RCPSP\benchmark_results"
OUTPUT_XLSX = ""  # leave blank for timestamped file in OUTPUT_DIR

SCREEN_ONE_PER_GROUP = True
EXPECTED_GROUPS = 48
GROUP_NUMBERS = None       # e.g. [1,2,3], or None for all 1..48
SAMPLE_INDICES = [1]

GAMMA_LIST = [30]
ALPHA_LIST = [0.5]
TIME_LIMIT = 600
METHODS_TO_RUN = ["layered_resourceflow"]

# Gurobi settings. For formal sequential experiments, THREADS=0 is OK, but
# if you want to leave CPU for browsing/typing, use THREADS=4 or 6.
THREADS = 4
GUROBI_OUTPUT = 0
MIP_FOCUS = 2
HEURISTICS_LEVEL = 0.05
PRESOLVE = -1
CUTS = -1
SEED = 1

ADD_TRANSITIVITY = True
ADD_PAIRWISE_CONFLICTS = True
STOP_AFTER_FIRST_ERROR = False
DASHBOARD_ENABLED = True
# v3: popup = external Tkinter window; terminal = old terminal dashboard; off = no live UI.
UI_MODE = "popup"
LAUNCH_POPUP_UI = True
REFRESH_INTERVAL_SEC = 0.5
SAVE_AFTER_EVERY_JOB = True
# =============================================================================

STATUS_NAME = {
    GRB.OPTIMAL: "OPTIMAL", GRB.INFEASIBLE: "INFEASIBLE", GRB.INF_OR_UNBD: "INF_OR_UNBD",
    GRB.UNBOUNDED: "UNBOUNDED", GRB.CUTOFF: "CUTOFF", GRB.ITERATION_LIMIT: "ITERATION_LIMIT",
    GRB.NODE_LIMIT: "NODE_LIMIT", GRB.TIME_LIMIT: "TIME_LIMIT", GRB.SOLUTION_LIMIT: "SOLUTION_LIMIT",
    GRB.INTERRUPTED: "INTERRUPTED", GRB.NUMERIC: "NUMERIC", GRB.SUBOPTIMAL: "SUBOPTIMAL",
}

HEADERS = [
    "run_id", "dataset", "method", "group", "sample_index", "gamma", "alpha", "time_limit", "file",
    "status", "status_name", "abnormal", "objective", "best_bound", "gap",
    "runtime_solver", "runtime_total", "build_time", "nodes", "sol_count", "vars", "constrs",
    "path_cuts", "mipsol_calls", "initial_lb", "error", "path", "start_time", "end_time", "last_update",
]


def apply_core_config() -> None:
    core.DATASET_ROOTS = DATASET_ROOTS
    core.DATASETS_TO_RUN = DATASETS_TO_RUN
    core.SCREEN_ONE_PER_GROUP = SCREEN_ONE_PER_GROUP
    core.SAMPLE_INDICES = SAMPLE_INDICES
    core.EXPECTED_GROUPS = EXPECTED_GROUPS
    core.GROUP_NUMBERS = GROUP_NUMBERS
    core.GAMMA_LIST = GAMMA_LIST
    core.ALPHA_LIST = ALPHA_LIST
    core.TIME_LIMIT = TIME_LIMIT
    core.METHODS_TO_RUN = METHODS_TO_RUN
    core.GUROBI_OUTPUT = GUROBI_OUTPUT
    core.MIP_FOCUS = MIP_FOCUS
    core.HEURISTICS_LEVEL = HEURISTICS_LEVEL
    core.PRESOLVE = PRESOLVE
    core.CUTS = CUTS
    core.THREADS = THREADS
    core.SEED = SEED
    core.ADD_TRANSITIVITY = ADD_TRANSITIVITY
    core.ADD_PAIRWISE_CONFLICTS = ADD_PAIRWISE_CONFLICTS
    core.PRINT_INSTANCE_LINE = False


def make_output_path() -> str:
    if OUTPUT_XLSX:
        return OUTPUT_XLSX
    stamp = time.strftime("%Y%m%d_%H%M%S")
    ds_tag = "-".join(DATASETS_TO_RUN)
    sample_tag = "s" + "-".join(str(x) for x in SAMPLE_INDICES)
    method_tag = "-".join(METHODS_TO_RUN)
    return os.path.join(OUTPUT_DIR, f"gurobi_{ds_tag}_{method_tag}_{sample_tag}_thr{THREADS}_{stamp}.xlsx")


def build_jobs() -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    selected_files: List[Tuple[str, str, str, int]] = []
    for dataset in DATASETS_TO_RUN:
        root = DATASET_ROOTS.get(dataset, "")
        instances = core.find_instances_for_dataset(dataset, root)
        for group, file_path, sample_index in instances:
            selected_files.append((dataset, group, file_path, sample_index))
    run_id = 0
    for dataset, group, file_path, sample_index in selected_files:
        for gamma in GAMMA_LIST:
            for alpha in ALPHA_LIST:
                for method in METHODS_TO_RUN:
                    run_id += 1
                    jobs.append({
                        "run_id": run_id, "dataset": dataset, "group": group, "sample_index": sample_index,
                        "gamma": int(gamma), "alpha": float(alpha), "method": method,
                        "time_limit": TIME_LIMIT, "file": file_path,
                    })
    return jobs


def normalize_result(job: Dict[str, Any], res: Dict[str, Any], started: str) -> Dict[str, Any]:
    out = {h: None for h in HEADERS}
    out.update(job)
    for k, v in res.items():
        if k in out:
            out[k] = v
    out["runtime_total"] = res.get("total_time", res.get("runtime_total"))
    out["file"] = res.get("file", job.get("file"))
    out["path"] = res.get("path", job.get("file"))
    out["time_limit"] = job.get("time_limit")
    out["start_time"] = started
    out["end_time"] = now_str()
    out["last_update"] = out["end_time"]
    out["abnormal"] = bool(res.get("abnormal", str(res.get("status_name")) != "OPTIMAL"))
    return out


def main() -> None:
    apply_core_config()
    jobs = build_jobs()
    output_path = make_output_path()
    run_info = RunInfo(
        name="Gurobi robust RCPSP library",
        method="gurobi_baselines",
        dataset=",".join(DATASETS_TO_RUN),
        output_path=output_path,
        total_jobs=len(jobs),
        time_limit=TIME_LIMIT,
        threads=THREADS,
        gamma_list=GAMMA_LIST,
        alpha_list=ALPHA_LIST,
        group_range=f"1..{EXPECTED_GROUPS}" if GROUP_NUMBERS is None else str(GROUP_NUMBERS),
        sample_indices=SAMPLE_INDICES,
    )
    config = {
        "script": "run_lg.py",
        "dataset_roots": DATASET_ROOTS,
        "datasets_to_run": DATASETS_TO_RUN,
        "screen_one_per_group": SCREEN_ONE_PER_GROUP,
        "expected_groups": EXPECTED_GROUPS,
        "group_numbers": GROUP_NUMBERS,
        "sample_indices": SAMPLE_INDICES,
        "gamma_list": GAMMA_LIST,
        "alpha_list": ALPHA_LIST,
        "time_limit": TIME_LIMIT,
        "methods_to_run": METHODS_TO_RUN,
        "threads": THREADS,
        "gurobi_output": GUROBI_OUTPUT,
        "mip_focus": MIP_FOCUS,
        "heuristics_level": HEURISTICS_LEVEL,
        "add_transitivity": ADD_TRANSITIVITY,
        "add_pairwise_conflicts": ADD_PAIRWISE_CONFLICTS,
        "ui_mode": UI_MODE,
        "launch_popup_ui": LAUNCH_POPUP_UI,
    }
    writer = ExcelReport(output_path, run_info, HEADERS, ["dataset", "method", "gamma", "alpha", "time_limit"], config)
    dashboard = create_dashboard(run_info, mode=UI_MODE, refresh_interval=REFRESH_INTERVAL_SEC, enabled=DASHBOARD_ENABLED, launch_popup=LAUNCH_POPUP_UI)
    data_cache: Dict[str, Any] = {}
    done = 0
    dashboard.draw(done, 0, None, force=True)

    for idx, job in enumerate(jobs, start=1):
        started = now_str()
        current = dict(job)
        current.update({"elapsed": 0.0, "status_name": "RUNNING", "objective": None, "best_bound": None, "gap": None})
        writer.add_or_update({**current, "start_time": started, "last_update": started}, force_save=True)
        t0 = time.time()
        dashboard.draw(done, idx, current, force=True)
        stop_event = threading.Event()

        def monitor() -> None:
            # This monitor only redraws elapsed time. It does not run another Gurobi model
            # and does not touch Gurobi objects, so the solve remains sequential.
            while not stop_event.is_set():
                current["elapsed"] = time.time() - t0
                dashboard.draw(done, idx, current, force=False)
                time.sleep(0.5)

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        try:
            file_path = job["file"]
            if file_path not in data_cache:
                data_cache[file_path] = core.load_psplib_sm(file_path)
            data = data_cache[file_path]
            # Core solver is sequential and blocks until current job finishes.
            # We deliberately do not run several jobs at once, to keep runtimes fair.
            last_progress_draw = {"t": 0.0}

            def progress_cb(info: Dict[str, Any]) -> None:
                now = time.time()
                if now - last_progress_draw["t"] < REFRESH_INTERVAL_SEC:
                    return
                last_progress_draw["t"] = now
                current.update(info)
                current["elapsed"] = now - t0
                dashboard.draw(done, idx, current, force=False)

            res = core.solve_one(
                job["method"], job["dataset"], file_path, data, str(job["group"]),
                int(job["sample_index"]), int(job["gamma"]), float(job["alpha"]),
                progress_cb=progress_cb,
            )
            final = normalize_result(job, res, started)
        except Exception as exc:
            if STOP_AFTER_FIRST_ERROR:
                raise
            final = {h: None for h in HEADERS}
            final.update(job)
            final.update({
                "status_name": "ERROR", "abnormal": True, "error": repr(exc) + "\n" + traceback.format_exc(limit=5),
                "runtime_total": time.time() - t0, "start_time": started, "end_time": now_str(), "last_update": now_str(),
                "file": job.get("file"), "path": job.get("file"),
            })
        stop_event.set()
        try:
            monitor_thread.join(timeout=1.0)
        except Exception:
            pass
        done += 1
        writer.add_or_update(final, force_save=True)
        writer.rebuild_all_summaries(); writer.save(force=True)
        dashboard.add_completed(final)
        dashboard.draw(done, idx, {**final, "elapsed": final.get("runtime_total")}, force=True)

    writer.rebuild_all_summaries(); writer.save(force=True)
    dashboard.draw(done, len(jobs), None, force=True)
    print(f"\nFinished. Excel saved: {output_path}")
    if UI_MODE.lower() in ("popup", "external", "gui", "window"):
        print("Popup UI reads the live status JSON next to the Excel file. You can close the popup window manually.")


if __name__ == "__main__":
    main()
