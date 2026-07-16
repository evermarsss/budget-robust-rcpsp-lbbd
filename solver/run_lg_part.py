# -*- coding: utf-8 -*-
"""
CLI wrapper for run_lg.py.

Use this file to run one sample-index block for the direct LG-baseline.
"""
from __future__ import annotations

import argparse
import os
from typing import List, Optional

import run_lg as runner


def parse_int_list(text: str) -> List[int]:
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
    ap = argparse.ArgumentParser(description="Run the direct LG-baseline on a selected sample-index block.")
    ap.add_argument("--dataset", default=runner.DATASETS_TO_RUN[0] if runner.DATASETS_TO_RUN else "J60")
    ap.add_argument("--root", default=next(iter(runner.DATASET_ROOTS.values())))
    ap.add_argument("--groups", default="1-48", help="Group range/list, e.g. 1-48")
    ap.add_argument("--samples", default=','.join(map(str, runner.SAMPLE_INDICES)), help="Sample indices, e.g. 1-3 or 7-10")
    ap.add_argument("--gamma", default=','.join(map(str, runner.GAMMA_LIST)))
    ap.add_argument("--alpha", default=','.join(map(str, runner.ALPHA_LIST)))
    ap.add_argument("--time-limit", type=float, default=float(runner.TIME_LIMIT))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--output-dir", default=runner.OUTPUT_DIR)
    ap.add_argument("--output", default="")
    ap.add_argument("--ui", default=runner.UI_MODE, choices=["popup", "terminal", "off"])
    ap.add_argument("--gurobi-log", type=int, default=0)
    args = ap.parse_args()

    groups = parse_int_list(args.groups)
    samples = parse_int_list(args.samples)
    gammas = parse_int_list(args.gamma)
    alphas = [float(x) for x in str(args.alpha).replace(' ', '').split(',') if x]
    runner.DATASET_ROOTS = {args.dataset: args.root}
    runner.DATASETS_TO_RUN = [args.dataset]
    runner.EXPECTED_GROUPS = max(groups)
    runner.GROUP_NUMBERS = groups
    runner.SCREEN_ONE_PER_GROUP = False
    runner.SAMPLE_INDICES = samples
    runner.GAMMA_LIST = gammas
    runner.ALPHA_LIST = alphas
    runner.TIME_LIMIT = float(args.time_limit)
    runner.THREADS = int(args.threads)
    runner.METHODS_TO_RUN = ["layered_resourceflow"]
    runner.OUTPUT_DIR = args.output_dir
    runner.OUTPUT_XLSX = args.output
    runner.UI_MODE = args.ui
    runner.LAUNCH_POPUP_UI = (args.ui.lower() == "popup")
    runner.GUROBI_OUTPUT = int(args.gurobi_log)

    os.makedirs(args.output_dir, exist_ok=True)
    runner.main()


if __name__ == "__main__":
    main()
