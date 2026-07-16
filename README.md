# Code for the robust RCPSP paper

This repository contains the implementation used for the computational study in
the accompanying paper on a budget-robust resource-constrained project
scheduling problem (RCPSP).  It deliberately contains **two methods only**:

| Label in the paper | Code entry point | Role |
| --- | --- | --- |
| `LG-baseline` | `solver/run_lg_part.py` | Direct compact layered resource-flow formulation solved by Gurobi. |
| `LBBD-main` | `solver/run_lbbd_part.py` | Proposed asymmetric logic-based Benders decomposition. |

The repository contains no third comparator, no experimental outputs, and no
PSPLIB instances.  The instances must be obtained separately from PSPLIB and
kept outside this repository.

## Methods represented by the code

### LG-baseline

`LG-baseline` is the direct compact formulation used as the Gurobi benchmark.
It jointly contains (i) binary precedence/ordering variables, (ii) explicit
renewable-resource flow variables, and (iii) layered robust timing variables
for the budgeted-duration uncertainty set.  Transitivity constraints and the
pairwise-conflict inequalities are enabled in the reported configuration.

This direct formulation follows the compact layered resource-flow modelling
paradigm of Bold and Goerigk (2021), while it is derived and implemented here
for the formulation stated in the accompanying paper.  In particular, the
implementation uses the tighter arc flow upper bound
`min(q[i,k], q[j,k]) * sigma[i,j]`.  Thus, `LG-baseline` should be described as
a direct compact *BG-type* benchmark, not as a verbatim reproduction of another
author's code.

### LBBD-main

`LBBD-main` is the proposed end-to-end algorithm.  Its master contains the
ordering variables and the layered robust makespan formulation.  Renewable
resource feasibility is checked on the transitive closure of a candidate order
by a Dinic maximum-flow separation routine.  An infeasible candidate produces
minimal-forbidden-set (MFS) lazy feasibility cuts.  The implementation also
uses the robust-path cuts configured in `run_lbbd.py`.

The reported implementation initializes LBBD-main with a serial schedule
generation scheme (SGS): the SGS solution supplies an incumbent ordering, a
Gurobi MIP start, and an initial makespan cutoff.  This is part of the proposed
algorithmic framework, not an option added to LG-baseline.  LG-baseline is run
as a direct Gurobi formulation without an SGS MIP start.  The comparison is
therefore between the complete direct formulation and the complete proposed
LBBD framework, exactly as implemented in this repository.

## Requirements

* Python 3.11 (the reported runs used Python 3.11.9)
* Gurobi 12.0.3 and a valid Gurobi license
* Packages in `requirements.txt`
* PSPLIB `.sm` instances, e.g. `j301_1.sm`

Install the Python packages from the repository root:

```bash
python -m pip install -r requirements.txt
```

## Reproducing a run

Run commands from `solver/`.  Replace `/path/to/j30` with the directory that
contains the corresponding `.sm` files.  The following commands run the 48
representative J30 instances with `Gamma=15`, `alpha=0.5`, a 600-second limit,
and two Gurobi threads:

```bash
cd solver
python run_lg_part.py --dataset J30 --root /path/to/j30 --groups 1-48 --samples 1 --gamma 15 --alpha 0.5 --time-limit 600 --threads 2 --ui off --output-dir ../results/lg_j30
python run_lbbd_part.py --dataset J30 --root /path/to/j30 --groups 1-48 --samples 1 --gamma 15 --alpha 0.5 --time-limit 600 --threads 2 --ui off --output-dir ../results/lbbd_j30
```

For the main experiment, use `Gamma = 15, 30, 45`, `alpha = 0.5`, and the
time limits reported in the paper (J30: 600 s; J60: 1200 s; J90: 1800 s).
Each runner writes an Excel workbook incrementally, so interrupted runs retain
completed rows.  `--ui off` is recommended for unattended runs.

The implementation fixes Gurobi's random seed to 1.  Runtime can nevertheless
vary across machines, Gurobi releases, and licenses; comparisons should use the
same machine, thread count, time limit, and instance files for both methods.

## File map

* `solver/direct_lg_baseline.py` — direct LG-baseline model and parser.
* `solver/run_lg.py`, `solver/run_lg_part.py` — LG-baseline batch runner and
  command-line wrapper.
* `solver/lbbd_main.py` — LBBD-main master, max-flow separation, and callbacks.
* `solver/run_lbbd.py`, `solver/run_lbbd_part.py` — LBBD-main batch runner and
  command-line wrapper.
* `solver/benchmark_ui_common.py`, `solver/benchmark_monitor_gui.py` — optional
  progress display and Excel-report utilities.

## Reference for the compact formulation family

M. Bold and M. Goerigk (2021), “A compact reformulation of the two-stage robust
resource-constrained project scheduling problem,” *Computers & Operations
Research*, 130, 105232.  The reference motivates the compact formulation family;
the paper accompanying this repository should be cited for the exact model,
the proposed LBBD-main algorithm, and its computational results.
