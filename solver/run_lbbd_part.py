# -*- coding: utf-8 -*-
"""
CLI wrapper for run_lbbd.py.

Use this file to run one sample-index block, e.g. samples 1-3, 4-6, 7-10.
It modifies the imported runner's global configuration and then calls runner.main().
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List, Optional

import run_lbbd as runner


def parse_int_list(text: str) -> List[int]:
    """Parse '1-3,7,9-10' into [1,2,3,7,9,10]."""
    out: List[int] = []
    for part in str(text).replace(" ", "").split(','):
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(dict.fromkeys(out))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run LBBD-main on a selected sample-index block.")
    ap.add_argument("--dataset", default=runner.DATASET_NAME, help="Dataset name, e.g. J30/J60/J90")
    ap.add_argument("--root", default=runner.DATASET_ROOT, help="Folder containing .sm files")
    ap.add_argument("--groups", default=f"{runner.GROUP_START}-{runner.GROUP_END}", help="Group range/list, e.g. 1-48")
    ap.add_argument("--samples", default=','.join(map(str, runner.SAMPLE_INDICES)), help="Sample indices, e.g. 1-3 or 7-10")
    ap.add_argument("--gamma", default=','.join(map(str, runner.GAMMA_LIST)), help="Gamma list, e.g. 15 or 15,20")
    ap.add_argument("--alpha", default=','.join(map(str, runner.ALPHA_LIST)), help="Alpha list, e.g. 0.5")
    ap.add_argument("--time-limit", type=float, default=runner.TIME_LIMIT_LIST[0] if runner.TIME_LIMIT_LIST else 600)
    ap.add_argument("--threads", type=int, default=4, help="Gurobi Threads for each process; recommended 2 or 4")
    ap.add_argument("--output-dir", default=runner.OUTPUT_DIR)
    ap.add_argument("--output", default="", help="Exact output xlsx path; optional")
    ap.add_argument("--ui", default=runner.UI_MODE, choices=["popup", "terminal", "off"], help="Live UI mode")
    ap.add_argument("--gurobi-log", type=int, default=0, help="Gurobi OutputFlag")
    args = ap.parse_args()

    groups = parse_int_list(args.groups)
    samples = parse_int_list(args.samples)
    gammas = parse_int_list(args.gamma)
    alphas = [float(x) for x in str(args.alpha).replace(' ', '').split(',') if x]

    runner.DATASET_NAME = args.dataset
    runner.DATASET_ROOT = args.root
    runner.GROUP_START = min(groups)
    runner.GROUP_END = max(groups)
    # LBBD runner uses a continuous group range. Keep this wrapper for 1..48 style groups.
    runner.SAMPLE_INDICES = samples
    runner.GAMMA_LIST = gammas
    runner.ALPHA_LIST = alphas
    runner.TIME_LIMIT_LIST = [float(args.time_limit)]
    runner.OUTPUT_DIR = args.output_dir
    runner.OUTPUT_XLSX = args.output
    runner.UI_MODE = args.ui
    runner.LAUNCH_POPUP_UI = (args.ui.lower() == "popup")
    runner.ALGORITHM_CONFIG["THREADS"] = int(args.threads)
    runner.ALGORITHM_CONFIG["GUROBI_OUTPUT"] = int(args.gurobi_log)

    os.makedirs(args.output_dir, exist_ok=True)
    runner.main()


if __name__ == "__main__":
    main()
