# -*- coding: utf-8 -*-
"""
Direct LG-baseline for the budget-robust RCPSP on PSPLIB instances.

What this script does
---------------------
You edit only the CONFIG block below, then run this module or run_lg_part.py.

It can screen representative instances or run all instances for J30/J60/J90.
For each selected instance, it can run multiple Gamma values and multiple Gurobi
baseline, then incrementally writes results to a single Excel .xlsx file.

Recommended use now
-------------------
1) Screening library:
   - SCREEN_ONE_PER_GROUP = True
   - SAMPLE_INDICES = [1]
   - DATASETS_TO_RUN = ["J30", "J60", "J90"]
   - GAMMA_LIST = [5, 10, 20]
   This runs 48 representatives per dataset and Gamma.

2) Later appendix/full library:
   - SCREEN_ONE_PER_GROUP = False
   - SAMPLE_INDICES = list(range(1, 11))
   This runs all 480 instances per dataset.

Implemented method
------------------
layered_resourceflow (LG-baseline)
   Direct compact layered resource-flow formulation solved by Gurobi.

Dependencies
------------
- gurobipy
- psplib  (pip install psplib)
No openpyxl is required. The script writes a minimal .xlsx with the standard library.
"""
from __future__ import annotations

import os
import re
import time
import math
import traceback
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

import gurobipy as gp
from gurobipy import GRB

# =============================================================================
# CONFIG：通常只改这里，不需要命令行参数
# =============================================================================

# 数据集根目录。没有的集合可以留空字符串或不放进 DATASETS_TO_RUN。
DATASET_ROOTS = {
    "J30": r"C:\Users\17717\Downloads\j30.sm (2)",
    "J60": r"C:\Users\17717\Downloads\j60.sm",
    "J90": r"C:\Users\17717\Downloads\j90.sm",
}

# 要跑哪些数据集。先筛选可只跑 ["J30"]，后面再加 J60/J90。
DATASETS_TO_RUN = ["J30"]

# 输出 Excel 文件。程序每跑完一个实例-方法-Gamma组合都会保存一次，防止中途断电丢结果。
OUTPUT_XLSX = r"C:\Users\17717\Documents\GitHub\RCPSP\最新BENDERS论文\results\gurobi_benchmark_library.xlsx"

# 运行模式：True = 每个 group 取 SAMPLE_INDICES 中的代表；False = 根据 SAMPLE_INDICES 跑更多/全部实例。
# 例如 True + SAMPLE_INDICES=[1]：J30 跑 j301_1,...,j3048_1，共 48 个。
# 例如 False + SAMPLE_INDICES=list(range(1, 11))：J30 跑 480 个。
SCREEN_ONE_PER_GROUP = True
SAMPLE_INDICES = [1]
EXPECTED_GROUPS = 48

# 若只想先跑部分 group，可填如 [1,2,3,4,41,42]；None 表示 1..EXPECTED_GROUPS 全部。
GROUP_NUMBERS: Optional[List[int]] = None

# 鲁棒参数。会对每个 Gamma 都跑一遍。
GAMMA_LIST = [20]
ALPHA_LIST = [0.5]

# 每个 instance-method-Gamma 的时间限制。筛异常建议 120；正式实验可改 600/3600。
TIME_LIMIT = 120

# 本模块只包含 LG-baseline。
METHODS_TO_RUN = ["layered_resourceflow"]

# LG-baseline model settings.
ADD_TRANSITIVITY = True
ADD_PAIRWISE_CONFLICTS = True

# Gurobi 参数
GUROBI_OUTPUT = 0
MIP_FOCUS = 2
HEURISTICS_LEVEL = 0.05
PRESOLVE = -1          # -1 使用默认
CUTS = -1              # -1 使用默认
THREADS = 0            # 0 使用默认
SEED = 1

# 异常判定：筛选阶段 120s 未 OPTIMAL 即 abnormal。
ABNORMAL_IF_NOT_OPTIMAL = True
ABNORMAL_GAP_TOL = 1e-4

# 是否跳过已经存在于 Excel/CSV 的结果。当前 minimal XLSX 不读取旧结果；这里保留配置位，默认 False。
SKIP_EXISTING = False

# 控制台输出
PRINT_INSTANCE_LINE = True
PRINT_TRACEBACK_ON_ERROR = False
# =============================================================================


@dataclass
class RCPSPData:
    n: int
    durations: List[float]
    demands: List[List[float]]
    capacities: List[float]
    renewable: List[int]
    successors: List[List[int]]
    source: int
    sink: int


def load_psplib_sm(path: str) -> RCPSPData:
    try:
        from psplib import parse
    except Exception as exc:
        raise RuntimeError("Please install psplib first: pip install psplib") from exc

    inst = parse(path, instance_format="psplib")
    acts = inst.activities
    res = inst.resources
    n = len(acts)

    durations: List[float] = []
    demands: List[List[float]] = []
    for a in acts:
        if not a.modes:
            durations.append(0.0)
            demands.append([0.0 for _ in res])
        else:
            durations.append(float(a.modes[0].duration))
            demands.append([float(x) for x in a.modes[0].demands])

    capacities = [float(r.capacity) for r in res]
    renewable = [k for k, r in enumerate(res) if getattr(r, "renewable", True)]
    successors = [[] for _ in range(n)]
    for i, a in enumerate(acts):
        for j in a.successors:
            jj = int(j)
            if 0 <= jj < n and jj != i:
                successors[i].append(jj)

    return RCPSPData(n, durations, demands, capacities, renewable, successors, 0, n - 1)


def topo_order_or_none(n: int, adj: List[List[int]]) -> Optional[List[int]]:
    indeg = [0] * n
    for i in range(n):
        for j in adj[i]:
            indeg[j] += 1
    q = [i for i in range(n) if indeg[i] == 0]
    out: List[int] = []
    head = 0
    while head < len(q):
        i = q[head]
        head += 1
        out.append(i)
        for j in adj[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                q.append(j)
    return out if len(out) == n else None


def robust_longest_path_dp(
    n: int,
    source: int,
    sink: int,
    selected_arcs: List[Tuple[int, int]],
    p: List[float],
    dev: List[float],
    gamma: int,
) -> Tuple[float, List[int], List[Tuple[int, int]]]:
    """Layered DP separator for budgeted robust longest path."""
    adj: List[List[int]] = [[] for _ in range(n)]
    for i, j in selected_arcs:
        if i != j:
            adj[i].append(j)
    order = topo_order_or_none(n, adj)
    if order is None:
        raise RuntimeError("Selected sigma contains a directed cycle; turn ADD_TRANSITIVITY on.")

    neg = -1.0e100
    gamma = int(gamma)
    D = [[neg] * (gamma + 1) for _ in range(n)]
    pred: List[List[Optional[Tuple[int, int, bool]]]] = [[None] * (gamma + 1) for _ in range(n)]
    D[source][0] = 0.0

    for i in order:
        for g in range(gamma + 1):
            base = D[i][g]
            if base <= neg / 2:
                continue
            for j in adj[i]:
                val = base + p[i]
                if val > D[j][g] + 1e-9:
                    D[j][g] = val
                    pred[j][g] = (i, g, False)
                if g < gamma:
                    val2 = base + p[i] + dev[i]
                    if val2 > D[j][g + 1] + 1e-9:
                        D[j][g + 1] = val2
                        pred[j][g + 1] = (i, g, True)

    best_g = max(range(gamma + 1), key=lambda gg: D[sink][gg])
    best_val = D[sink][best_g]
    if best_val <= neg / 2:
        raise RuntimeError("No source--sink path in selected sigma.")

    nodes_rev = [sink]
    cur, g = sink, best_g
    while cur != source:
        pr = pred[cur][g]
        if pr is None:
            raise RuntimeError("Failed to recover robust critical path.")
        prev, pg, _ = pr
        nodes_rev.append(prev)
        cur, g = prev, pg
    path_nodes = list(reversed(nodes_rev))
    path_arcs = list(zip(path_nodes[:-1], path_nodes[1:]))
    return best_val, path_nodes, path_arcs


def robust_cpm_lb(data: RCPSPData, p: List[float], dev: List[float], gamma: int) -> float:
    arcs = [(i, j) for i in range(data.n) for j in data.successors[i]]
    val, _, _ = robust_longest_path_dp(data.n, data.source, data.sink, arcs, p, dev, gamma)
    return val


def status_name(status: Optional[int]) -> str:
    if status is None:
        return "NONE"
    names = {
        GRB.OPTIMAL: "OPTIMAL",
        GRB.INFEASIBLE: "INFEASIBLE",
        GRB.INF_OR_UNBD: "INF_OR_UNBD",
        GRB.UNBOUNDED: "UNBOUNDED",
        GRB.TIME_LIMIT: "TIME_LIMIT",
        GRB.NODE_LIMIT: "NODE_LIMIT",
        GRB.INTERRUPTED: "INTERRUPTED",
        GRB.SUBOPTIMAL: "SUBOPTIMAL",
        GRB.NUMERIC: "NUMERIC",
    }
    return names.get(status, str(status))


def base_result(dataset: str, method: str, file_path: str, group: str, sample: int, gamma: int, alpha: float) -> Dict[str, Any]:
    return {
        "dataset": dataset,
        "method": method,
        "group": group,
        "sample_index": sample,
        "gamma": gamma,
        "alpha": alpha,
        "file": os.path.basename(file_path),
        "path": file_path,
        "status": None,
        "status_name": None,
        "abnormal": True,
        "objective": None,
        "best_bound": None,
        "gap": None,
        "runtime_solver": None,
        "total_time": None,
        "build_time": None,
        "nodes": None,
        "sol_count": None,
        "vars": None,
        "constrs": None,
        "path_cuts": None,
        "mipsol_calls": None,
        "initial_lb": None,
        "error": "",
    }




def make_silent_model(name: str) -> gp.Model:
    """Create a Gurobi model without printing license/parameter messages.

    Setting OutputFlag after gp.Model(...) is too late on Windows because the
    environment may already print license information. Creating an empty Env and
    setting OutputFlag/LogToConsole before env.start() keeps the terminal UI clean.
    """
    if GUROBI_OUTPUT:
        return gp.Model(name)
    env = gp.Env(empty=True)
    env.setParam("OutputFlag", 0)
    try:
        env.setParam("LogToConsole", 0)
    except Exception:
        pass
    env.start()
    m = gp.Model(name, env=env)
    m._owned_env = env
    return m


def fill_gurobi_result(res: Dict[str, Any], m: gp.Model, build_time: float, total_time: float) -> None:
    res["status"] = int(m.Status)
    res["status_name"] = status_name(m.Status)
    res["runtime_solver"] = float(getattr(m, "Runtime", total_time))
    res["total_time"] = float(total_time)
    res["build_time"] = float(build_time)
    res["nodes"] = float(getattr(m, "NodeCount", 0.0))
    res["sol_count"] = int(m.SolCount)
    res["vars"] = int(m.NumVars)
    res["constrs"] = int(m.NumConstrs)
    if m.SolCount > 0:
        res["objective"] = float(m.ObjVal)
        res["best_bound"] = float(m.ObjBound)
        res["gap"] = float((m.ObjVal - m.ObjBound) / max(1.0, abs(m.ObjVal)))
    else:
        try:
            res["best_bound"] = float(m.ObjBound)
        except Exception:
            pass
        res["gap"] = None
    if ABNORMAL_IF_NOT_OPTIMAL:
        res["abnormal"] = (m.Status != GRB.OPTIMAL)
    else:
        g = res.get("gap")
        res["abnormal"] = (g is None or g > ABNORMAL_GAP_TOL)


def set_gurobi_params(m: gp.Model) -> None:
    m.Params.TimeLimit = TIME_LIMIT
    m.Params.OutputFlag = GUROBI_OUTPUT
    m.Params.MIPFocus = MIP_FOCUS
    m.Params.Heuristics = HEURISTICS_LEVEL
    m.Params.Seed = SEED
    if PRESOLVE != -1:
        m.Params.Presolve = PRESOLVE
    if CUTS != -1:
        m.Params.Cuts = CUTS
    if THREADS:
        m.Params.Threads = THREADS




def make_progress_callback(progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None):
    """Return a lightweight Gurobi MIP callback for live UI progress.

    It only reads incumbent, bound, gap, node count, and solution count. It does
    not add cuts and does not run another optimization. The runner throttles UI
    writes, so this callback is safe for sequential benchmark monitoring.
    """
    if progress_cb is None:
        return None

    def _cb(model: gp.Model, where: int) -> None:
        if where != GRB.Callback.MIP:
            return
        try:
            best = model.cbGet(GRB.Callback.MIP_OBJBST)
            bound = model.cbGet(GRB.Callback.MIP_OBJBND)
            nodes = model.cbGet(GRB.Callback.MIP_NODCNT)
            solcnt = model.cbGet(GRB.Callback.MIP_SOLCNT)
            obj = None if solcnt <= 0 or abs(best) > 1e99 else float(best)
            bd = None if abs(bound) > 1e99 else float(bound)
            gap = None
            if obj is not None and bd is not None:
                gap = float((obj - bd) / max(1.0, abs(obj)))
            progress_cb({
                "objective": obj,
                "best_bound": bd,
                "gap": gap,
                "nodes": float(nodes),
                "sol_count": int(solcnt),
                "status_name": "RUNNING",
            })
        except Exception:
            pass

    return _cb


def add_common_resourceflow_model(m: gp.Model, data: RCPSPData, arcs: List[Tuple[int, int]]) -> Tuple[Any, Any]:
    n = data.n
    V = list(range(n))
    source, sink = data.source, data.sink
    real = [i for i in V if i not in (source, sink)]

    sigma = m.addVars(arcs, vtype=GRB.BINARY, name="sig")
    f = m.addVars([(i, j, k) for k in data.renewable for (i, j) in arcs], lb=0.0, name="f")
    m.update()

    for i in V:
        for j in data.successors[i]:
            if i != j:
                m.addConstr(sigma[i, j] == 1, name=f"orig_prec_{i}_{j}")

    for j in real:
        m.addConstr(sigma[source, j] == 1, name=f"src_before_{j}")
        m.addConstr(sigma[j, sink] == 1, name=f"before_sink_{j}")
        m.addConstr(sigma[j, source] == 0, name=f"no_into_src_{j}")
        m.addConstr(sigma[sink, j] == 0, name=f"no_out_sink_{j}")
    m.addConstr(sigma[source, sink] == 1, name="src_before_sink")
    m.addConstr(sigma[sink, source] == 0, name="sink_not_src")

    for i in V:
        for j in V:
            if i < j:
                m.addConstr(sigma[i, j] + sigma[j, i] <= 1, name=f"antisym_{i}_{j}")

    if ADD_TRANSITIVITY:
        for i in V:
            for k in V:
                if k == i:
                    continue
                for j in V:
                    if j == i or j == k:
                        continue
                    m.addConstr(sigma[i, j] >= sigma[i, k] + sigma[k, j] - 1,
                                name=f"trans_{i}_{k}_{j}")

    if ADD_PAIRWISE_CONFLICTS:
        added = set()
        for k in data.renewable:
            for a, i in enumerate(real):
                for j in real[a + 1:]:
                    if data.demands[i][k] + data.demands[j][k] > data.capacities[k]:
                        key = (i, j)
                        if key not in added:
                            m.addConstr(sigma[i, j] + sigma[j, i] >= 1,
                                        name=f"pair_conf_{i}_{j}")
                            added.add(key)

    for k in data.renewable:
        cap = float(data.capacities[k])

        def req(i: int) -> float:
            return cap if i in (source, sink) else float(data.demands[i][k])

        for i in [source] + real:
            m.addConstr(gp.quicksum(f[i, j, k] for j in V if j != i) == req(i),
                        name=f"flow_out_{i}_k{k}")
        for j in real + [sink]:
            m.addConstr(gp.quicksum(f[i, j, k] for i in V if i != j) == req(j),
                        name=f"flow_in_{j}_k{k}")
        for i, j in arcs:
            ub = min(req(i), req(j))
            m.addConstr(f[i, j, k] <= ub * sigma[i, j], name=f"flow_cap_{i}_{j}_k{k}")

    return sigma, f


def solve_layered_resourceflow(dataset: str, file_path: str, data: RCPSPData, group: str, sample: int, gamma: int, alpha: float, progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    method = "layered_resourceflow"
    res = base_result(dataset, method, file_path, group, sample, gamma, alpha)
    t0 = time.time()
    try:
        n = data.n
        V = list(range(n))
        source, sink = data.source, data.sink
        arcs = [(i, j) for i in V for j in V if i != j]
        layers = list(range(int(gamma) + 1))
        p = [float(x) for x in data.durations]
        dev = [0.0 if i in (source, sink) else float(alpha) * p[i] for i in V]
        big_m = sum(p[i] + dev[i] for i in V) + 1.0

        m = make_silent_model(f"layered_resourceflow_{dataset}_{group}_{sample}_G{gamma}")
        set_gurobi_params(m)
        sigma, _f = add_common_resourceflow_model(m, data, arcs)
        x = m.addVars([(i, g) for i in V for g in layers], lb=0.0, name="x")
        eta = m.addVar(lb=0.0, name="eta")
        m.setObjective(eta, GRB.MINIMIZE)
        m.update()

        for g in layers:
            m.addConstr(x[source, g] == 0.0, name=f"source_layer_{g}")
            m.addConstr(eta >= x[sink, g], name=f"eta_sink_layer_{g}")
        for i, j in arcs:
            for g in layers:
                m.addConstr(x[j, g] >= x[i, g] + p[i] - big_m * (1 - sigma[i, j]),
                            name=f"path_nom_{i}_{j}_{g}")
                if g < gamma:
                    m.addConstr(x[j, g + 1] >= x[i, g] + p[i] + dev[i] - big_m * (1 - sigma[i, j]),
                                name=f"path_dev_{i}_{j}_{g}")
        m.update()
        build_time = time.time() - t0
        cb = make_progress_callback(progress_cb)
        if cb is None:
            m.optimize()
        else:
            m.optimize(cb)
        total_time = time.time() - t0
        fill_gurobi_result(res, m, build_time, total_time)
    except Exception as exc:
        res["total_time"] = time.time() - t0
        res["error"] = str(exc)
        res["abnormal"] = True
        res["status_name"] = "ERROR"
        if PRINT_TRACEBACK_ON_ERROR:
            traceback.print_exc()
    return res


def dataset_prefix(dataset: str) -> str:
    m = re.search(r"(\d+)", dataset)
    if not m:
        raise ValueError(f"Cannot infer prefix from dataset name: {dataset}")
    return "j" + m.group(1)


def selected_group_numbers() -> List[int]:
    if GROUP_NUMBERS is None:
        return list(range(1, EXPECTED_GROUPS + 1))
    return list(GROUP_NUMBERS)


def find_instances_for_dataset(dataset: str, root_dir: str) -> List[Tuple[str, str, int]]:
    """Return [(group_name, filepath, sample_index)] according to CONFIG."""
    if not os.path.isdir(root_dir):
        print(f"[warning] dataset root does not exist for {dataset}: {root_dir}")
        return []
    prefix = dataset_prefix(dataset)  # e.g., J30 -> j30
    pat = re.compile(rf"^({re.escape(prefix)}(\d+))_(\d+)\.sm$", re.IGNORECASE)
    by_group: Dict[int, Dict[int, str]] = {}
    group_name: Dict[int, str] = {}
    for name in os.listdir(root_dir):
        m = pat.match(name)
        if not m:
            continue
        gname = m.group(1).lower()
        gnum = int(m.group(2))
        sidx = int(m.group(3))
        by_group.setdefault(gnum, {})[sidx] = os.path.join(root_dir, name)
        group_name[gnum] = gname

    selected: List[Tuple[str, str, int]] = []
    missing: List[str] = []
    for gnum in selected_group_numbers():
        if gnum not in by_group:
            missing.append(f"{prefix}{gnum}")
            continue
        files = by_group[gnum]
        if SCREEN_ONE_PER_GROUP:
            # In screening mode, use the first requested sample that exists; normally SAMPLE_INDICES=[1].
            chosen_idx = None
            for s in SAMPLE_INDICES:
                if s in files:
                    chosen_idx = s
                    break
            if chosen_idx is None:
                chosen_idx = sorted(files)[0]
                missing.append(f"{group_name[gnum]}_{SAMPLE_INDICES}.sm missing; used _{chosen_idx}")
            selected.append((group_name[gnum], files[chosen_idx], chosen_idx))
        else:
            for s in SAMPLE_INDICES:
                if s in files:
                    selected.append((group_name[gnum], files[s], s))
                else:
                    missing.append(f"{group_name[gnum]}_{s}.sm")
    if missing:
        print(f"[warning] {dataset} missing/fallback items:")
        for x in missing[:20]:
            print("  -", x)
        if len(missing) > 20:
            print(f"  ... {len(missing)-20} more")
    return selected


def xml_cell_ref(row: int, col: int) -> str:
    s = ""
    c = col
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return f"{s}{row}"


def write_simple_xlsx(path: str, sheets: Dict[str, List[List[Any]]]) -> None:
    """Write a minimal XLSX workbook using only the Python standard library."""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    def cell_xml(r: int, c: int, val: Any) -> str:
        ref = xml_cell_ref(r, c)
        if val is None:
            return f'<c r="{ref}"/>'
        if isinstance(val, bool):
            return f'<c r="{ref}" t="b"><v>{1 if val else 0}</v></c>'
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                txt = escape(str(val))
                return f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>'
            return f'<c r="{ref}"><v>{val}</v></c>'
        txt = escape(str(val))
        return f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>'

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
""" + "\n".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1)) + "\n</Types>")
        z.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""")
        z.writestr("docProps/core.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>direct_lg_baseline.py</dc:creator><cp:lastModifiedBy>direct_lg_baseline.py</cp:lastModifiedBy></cp:coreProperties>""")
        z.writestr("docProps/app.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Python</Application></Properties>""")
        z.writestr("xl/styles.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs></styleSheet>""")
        sheet_names = list(sheets.keys())
        workbook_sheets = "".join(f'<sheet name="{escape(name[:31])}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheet_names, start=1))
        z.writestr("xl/workbook.xml", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{workbook_sheets}</sheets></workbook>""")
        rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheet_names)+1))
        rels += f'<Relationship Id="rId{len(sheet_names)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        z.writestr("xl/_rels/workbook.xml.rels", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>""")
        for si, name in enumerate(sheet_names, start=1):
            rows = sheets[name]
            sheet_data = []
            for r_idx, row in enumerate(rows, start=1):
                cells = "".join(cell_xml(r_idx, c_idx, val) for c_idx, val in enumerate(row, start=1))
                sheet_data.append(f'<row r="{r_idx}">{cells}</row>')
            z.writestr(f"xl/worksheets/sheet{si}.xml", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheetData>{''.join(sheet_data)}</sheetData></worksheet>""")


def mean_or_none(values: List[Optional[float]]) -> Optional[float]:
    xs = [x for x in values if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def build_excel_sheets(results: List[Dict[str, Any]], selected_files: List[Tuple[str, str, str, int]]) -> Dict[str, List[List[Any]]]:
    headers = [
        "dataset", "method", "group", "sample_index", "gamma", "alpha", "file",
        "status", "status_name", "abnormal", "objective", "best_bound", "gap",
        "runtime_solver", "total_time", "build_time", "nodes", "sol_count", "vars", "constrs",
        "path_cuts", "mipsol_calls", "initial_lb", "error", "path"
    ]
    all_rows = [headers]
    for r in results:
        all_rows.append([r.get(h) for h in headers])

    abnormal = [headers]
    for r in results:
        if r.get("abnormal"):
            abnormal.append([r.get(h) for h in headers])

    # Summary by dataset-method-gamma-alpha
    summary_headers = ["dataset", "method", "gamma", "alpha", "instances", "optimal", "abnormal", "avg_time", "max_time", "avg_gap", "avg_nodes", "avg_path_cuts"]
    summary = [summary_headers]
    keys = sorted({(r.get("dataset"), r.get("method"), r.get("gamma"), r.get("alpha")) for r in results})
    for dataset, method, gamma, alpha in keys:
        subset = [r for r in results if (r.get("dataset"), r.get("method"), r.get("gamma"), r.get("alpha")) == (dataset, method, gamma, alpha)]
        optimal = sum(1 for r in subset if r.get("status") == GRB.OPTIMAL)
        abnormal_count = sum(1 for r in subset if r.get("abnormal"))
        times = [r.get("total_time") for r in subset]
        gaps = [r.get("gap") for r in subset]
        nodes = [r.get("nodes") for r in subset]
        pcs = [r.get("path_cuts") for r in subset]
        summary.append([dataset, method, gamma, alpha, len(subset), optimal, abnormal_count, mean_or_none(times), max([x for x in times if isinstance(x, (int, float))], default=None), mean_or_none(gaps), mean_or_none(nodes), mean_or_none(pcs)])

    # Summary by dataset-method only
    summary2_headers = ["dataset", "method", "instances", "optimal", "abnormal", "avg_time", "max_time", "avg_gap", "avg_nodes"]
    summary2 = [summary2_headers]
    keys2 = sorted({(r.get("dataset"), r.get("method")) for r in results})
    for dataset, method in keys2:
        subset = [r for r in results if (r.get("dataset"), r.get("method")) == (dataset, method)]
        optimal = sum(1 for r in subset if r.get("status") == GRB.OPTIMAL)
        abnormal_count = sum(1 for r in subset if r.get("abnormal"))
        times = [r.get("total_time") for r in subset]
        gaps = [r.get("gap") for r in subset]
        nodes = [r.get("nodes") for r in subset]
        summary2.append([dataset, method, len(subset), optimal, abnormal_count, mean_or_none(times), max([x for x in times if isinstance(x, (int, float))], default=None), mean_or_none(gaps), mean_or_none(nodes)])

    cfg = [
        ["parameter", "value"],
        ["DATASET_ROOTS", str(DATASET_ROOTS)],
        ["DATASETS_TO_RUN", str(DATASETS_TO_RUN)],
        ["OUTPUT_XLSX", OUTPUT_XLSX],
        ["SCREEN_ONE_PER_GROUP", SCREEN_ONE_PER_GROUP],
        ["SAMPLE_INDICES", str(SAMPLE_INDICES)],
        ["EXPECTED_GROUPS", EXPECTED_GROUPS],
        ["GROUP_NUMBERS", str(GROUP_NUMBERS)],
        ["GAMMA_LIST", str(GAMMA_LIST)],
        ["ALPHA_LIST", str(ALPHA_LIST)],
        ["TIME_LIMIT", TIME_LIMIT],
        ["METHODS_TO_RUN", str(METHODS_TO_RUN)],
        ["ADD_TRANSITIVITY", ADD_TRANSITIVITY],
        ["ADD_PAIRWISE_CONFLICTS", ADD_PAIRWISE_CONFLICTS],
        ["MIP_FOCUS", MIP_FOCUS],
        ["HEURISTICS_LEVEL", HEURISTICS_LEVEL],
        ["PRESOLVE", PRESOLVE],
        ["CUTS", CUTS],
        ["THREADS", THREADS],
        ["SEED", SEED],
    ]
    files = [["dataset", "group", "sample_index", "file", "path"]] + [[ds, g, sidx, os.path.basename(fp), fp] for ds, g, fp, sidx in selected_files]
    return {"summary": summary, "summary_method": summary2, "all_results": all_rows, "abnormal": abnormal, "config": cfg, "selected_files": files}


def solve_one(method: str, dataset: str, file_path: str, data: RCPSPData, group: str, sample: int, gamma: int, alpha: float, progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    if method == "layered_resourceflow":
        return solve_layered_resourceflow(dataset, file_path, data, group, sample, gamma, alpha, progress_cb=progress_cb)
    raise ValueError(f"Unknown method: {method}")


def main() -> None:
    print("========== Robust RCPSP Gurobi benchmark library ==========")
    print(f"DATASETS_TO_RUN       : {DATASETS_TO_RUN}")
    print(f"OUTPUT_XLSX           : {OUTPUT_XLSX}")
    print(f"SCREEN_ONE_PER_GROUP  : {SCREEN_ONE_PER_GROUP}")
    print(f"SAMPLE_INDICES        : {SAMPLE_INDICES}")
    print(f"GAMMA_LIST            : {GAMMA_LIST}")
    print(f"ALPHA_LIST            : {ALPHA_LIST}")
    print(f"METHODS_TO_RUN        : {METHODS_TO_RUN}")
    print(f"TIME_LIMIT            : {TIME_LIMIT}s")
    print("===========================================================")

    selected_files: List[Tuple[str, str, str, int]] = []
    for dataset in DATASETS_TO_RUN:
        root = DATASET_ROOTS.get(dataset, "")
        instances = find_instances_for_dataset(dataset, root)
        print(f"{dataset}: selected {len(instances)} instances from {root}")
        for group, fp, sidx in instances:
            selected_files.append((dataset, group, fp, sidx))

    total_jobs = len(selected_files) * len(GAMMA_LIST) * len(ALPHA_LIST) * len(METHODS_TO_RUN)
    print(f"Total solve jobs: {total_jobs}")

    results: List[Dict[str, Any]] = []
    t_all = time.time()
    job = 0
    data_cache: Dict[str, RCPSPData] = {}
    for dataset, group, file_path, sample in selected_files:
        if file_path not in data_cache:
            data_cache[file_path] = load_psplib_sm(file_path)
        data = data_cache[file_path]
        for gamma in GAMMA_LIST:
            for alpha in ALPHA_LIST:
                for method in METHODS_TO_RUN:
                    job += 1
                    print(f"\n[{job}/{total_jobs}] {dataset} {group}_{sample}.sm gamma={gamma} alpha={alpha} method={method}")
                    res = solve_one(method, dataset, file_path, data, group, sample, int(gamma), float(alpha))
                    results.append(res)
                    if PRINT_INSTANCE_LINE:
                        gap = res.get("gap")
                        gap_pct = None if gap is None else 100.0 * gap
                        t = res.get("total_time")
                        print(f"  status={res.get('status_name')} abnormal={res.get('abnormal')} "
                              f"obj={res.get('objective')} bd={res.get('best_bound')} gap%={gap_pct} "
                              f"time={t:.2f}s nodes={res.get('nodes')} cuts={res.get('path_cuts')} err={res.get('error')}")
                    sheets = build_excel_sheets(results, selected_files)
                    write_simple_xlsx(OUTPUT_XLSX, sheets)
                    print(f"  [saved] {OUTPUT_XLSX}")

    total = time.time() - t_all
    print("\n========== Done ==========")
    print(f"total wall time: {total:.2f}s")
    for method in METHODS_TO_RUN:
        subset = [r for r in results if r["method"] == method]
        opt = sum(1 for r in subset if r.get("status") == GRB.OPTIMAL)
        abn = sum(1 for r in subset if r.get("abnormal"))
        print(f"{method}: instances={len(subset)} optimal={opt} abnormal={abn}")
    print(f"Excel saved: {OUTPUT_XLSX}")


if __name__ == "__main__":
    main()
