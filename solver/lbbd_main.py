# -*- coding: utf-8 -*-
"""
Hybrid exact algorithm for robust RCPSP under budgeted duration uncertainty.

Proposed LBBD-main: compact robust optimality layer + branch-and-check MFS resource feasibility +
SP1-feasible robust critical-path optimality cuts.

Why this version exists
-----------------------
Pure LBBD struggled because robust optimality cuts are too weak: eta is only
strengthened at integer callback solutions.  The direct Gurobi resource-flow
model is fast on many j30 cases because it embeds all layered robust longest-path
constraints in the root relaxation, allowing presolve and generic cuts to lift the
bound.

This hybrid keeps the strong compact robust makespan formulation in the master,
and decomposes only the renewable-resource feasibility part by lazy MFS cuts.
Thus:
    - The layered constraints still define eta for integer sigma.
    - Callback checks SP1 resource feasibility and adds MFS cuts if infeasible.
    - If SP1 is feasible, a robust critical path is extracted and a globally
      valid conditional path optimality cut is stored in a cut pool.
    - At later MIPNODE callbacks, violated path cuts from this pool are added
      as user cuts, strengthening the LP relaxation without changing the
      original optimum.

Run directly after editing USER CONFIG.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import gurobipy as gp
from gurobipy import GRB

# =============================================================================
# USER CONFIG
# =============================================================================
FILE_PATH = r"C:\Users\17717\Downloads\j30.sm (2)\j301_1.sm"
GAMMA = 20
ALPHA = 0.5
TIME_LIMIT = 3600
GUROBI_OUTPUT = 1
THREADS = 0  # 0 = Gurobi default; set to 2/4 for parallel batch experiments

# Main switch: keep transitivity off first. Robust layered constraints with
# positive processing times already eliminate selected positive-duration cycles.
# Turning transitivity on makes sigma a closure relation and may strengthen some
# cuts, but adds O(n^3) rows.
ADD_TRANSITIVITY = False
ADD_PAIRWISE_CONFLICTS = True
USE_INITIAL_SGS_UB = True
SET_MIP_START_FROM_SGS = True
ADD_ETA_CUTOFF_FROM_SGS = True

# Callback / monitoring
PROGRESS_OUTPUT = True
PROGRESS_INTERVAL_SEC = 2
CACHE_SP_RESULTS = True
MAX_LAZY_PER_MIPSOL = 20
MIP_FOCUS = 2
HEURISTICS_LEVEL = 0.10
NOREL_HEUR_TIME = 5

# SP1-feasible robust critical-path optimality cuts.
# These are globally valid cuts of the form
#   eta + (L(P)-LB0) * sum_{(i,j) in P}(1-sigma_ij) >= L(P).
# They are generated when an integer solution passes SP1, stored in a pool,
# and injected as USER CUTS at MIPNODE when violated by fractional LP solutions.
ADD_PATH_OPT_CUTS = True
PATH_CUT_FROM_SP1_FEASIBLE = True
PATH_CUT_ADD_LAZY_IF_VIOLATED_AT_MIPSOL = True
PATH_CUT_CHECK_MIPNODE = True
PATH_CUT_MIPNODE_FREQ = 1          # check every Nth B&B node; use 5/10 if callback is heavy
PATH_CUT_POOL_MAX_SIZE = 2000
PATH_CUT_POOL_SCAN_LIMIT = 200     # scan most recent cuts at each checked MIPNODE
MAX_PATH_USER_CUTS_PER_NODE = 10
PATH_CUT_VIOL_TOL = 1e-5
PATH_CUT_MIN_COEFF = 1e-8

# SP1 infeasibility cuts.
ADD_MFS_CUTS = True
# Fallback full no-good if MFS separation fails. Keep on for correctness in rare cases.
USE_FULL_NOGOOD_FALLBACK = True

# Numerical tolerance
EPS = 1e-6
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


class Dinic:
    def __init__(self, n: int):
        self.n = n
        self.g: List[List[List[Any]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: float) -> None:
        cap = float(cap)
        fwd = [v, cap, None]
        rev = [u, 0.0, fwd]
        fwd[2] = rev
        self.g[u].append(fwd)
        self.g[v].append(rev)

    def max_flow(self, s: int, t: int, eps: float = 1e-9) -> float:
        flow = 0.0
        while True:
            level = [-1] * self.n
            q = deque([s])
            level[s] = 0
            while q:
                u = q.popleft()
                for e in self.g[u]:
                    if e[1] > eps and level[e[0]] < 0:
                        level[e[0]] = level[u] + 1
                        q.append(e[0])
            if level[t] < 0:
                break
            it = [0] * self.n

            def dfs(u: int, pushed: float) -> float:
                if u == t:
                    return pushed
                while it[u] < len(self.g[u]):
                    e = self.g[u][it[u]]
                    v, cap, rev = e
                    if cap > eps and level[v] == level[u] + 1:
                        tr = dfs(v, min(pushed, cap))
                        if tr > eps:
                            e[1] -= tr
                            rev[1] += tr
                            return tr
                    it[u] += 1
                return 0.0

            while True:
                pushed = dfs(s, float("inf"))
                if pushed <= eps:
                    break
                flow += pushed
        return flow

    def reachable_from(self, s: int, eps: float = 1e-9) -> Set[int]:
        seen = {s}
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e[1] > eps and e[0] not in seen:
                    seen.add(e[0])
                    q.append(e[0])
        return seen


def topological_order(n: int, edges: List[Tuple[int, int]]) -> Optional[List[int]]:
    succ = [[] for _ in range(n)]
    indeg = [0] * n
    for i, j in edges:
        if i == j:
            continue
        succ[i].append(j)
        indeg[j] += 1
    q = deque([i for i in range(n) if indeg[i] == 0])
    topo: List[int] = []
    while q:
        i = q.popleft()
        topo.append(i)
        for j in succ[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                q.append(j)
    return topo if len(topo) == n else None


def transitive_closure_from_edges(n: int, edges: List[Tuple[int, int]]) -> List[List[bool]]:
    reach = [[False] * n for _ in range(n)]
    for i, j in edges:
        if i != j:
            reach[i][j] = True
    # Floyd-Warshall with boolean rows.
    for k in range(n):
        row_k = reach[k]
        for i in range(n):
            if reach[i][k]:
                row_i = reach[i]
                for j in range(n):
                    if row_k[j]:
                        row_i[j] = True
    return reach


def transitive_closure_from_sigma(n: int, sigma: Dict[Tuple[int, int], int]) -> List[List[bool]]:
    return transitive_closure_from_edges(n, [(i, j) for (i, j), v in sigma.items() if v == 1])


def robust_longest_path_value(data: RCPSPData, sigma: Dict[Tuple[int, int], int], gamma: int, alpha: float) -> Optional[float]:
    """Independent integer-sigma verification oracle."""
    n = data.n
    p = data.durations
    dev = [0.0 if i in (data.source, data.sink) else alpha * p[i] for i in range(n)]
    edges = [(i, j) for (i, j), v in sigma.items() if v == 1]
    topo = topological_order(n, edges)
    if topo is None:
        return None
    succ = [[] for _ in range(n)]
    for i, j in edges:
        succ[i].append(j)
    neg = -1e100
    dp = [[neg for _ in range(gamma + 1)] for _ in range(n)]
    dp[data.source][0] = 0.0
    for i in topo:
        for g in range(gamma + 1):
            if dp[i][g] <= neg / 2:
                continue
            for j in succ[i]:
                dp[j][g] = max(dp[j][g], dp[i][g] + p[i])
                if g < gamma and dev[i] > 0:
                    dp[j][g + 1] = max(dp[j][g + 1], dp[i][g] + p[i] + dev[i])
    val = max(dp[data.sink])
    return None if val <= neg / 2 else float(val)



def robust_longest_path_certificate(
    data: RCPSPData,
    sigma: Dict[Tuple[int, int], int],
    gamma: int,
    alpha: float,
) -> Optional[Dict[str, Any]]:
    """Return a robust critical path certificate for an integer sigma.

    The DP is the same layered robust longest-path oracle used for verification,
    but predecessor labels are stored so that one critical path can be recovered.
    The returned path is a list of nodes from source to sink and `edges` is the
    corresponding list of ordering arcs.
    """
    n = data.n
    p = data.durations
    dev = [0.0 if i in (data.source, data.sink) else alpha * p[i] for i in range(n)]
    edges = [(i, j) for (i, j), v in sigma.items() if v == 1]
    topo = topological_order(n, edges)
    if topo is None:
        return None
    succ = [[] for _ in range(n)]
    for i, j in edges:
        succ[i].append(j)
    neg = -1e100
    dp = [[neg for _ in range(gamma + 1)] for _ in range(n)]
    prev: List[List[Optional[Tuple[int, int, int, int]]]] = [[None for _ in range(gamma + 1)] for _ in range(n)]
    # prev[j][g2] = (i, g1, used_dev, j)
    dp[data.source][0] = 0.0
    for i in topo:
        for g in range(gamma + 1):
            base = dp[i][g]
            if base <= neg / 2:
                continue
            for j in succ[i]:
                val = base + p[i]
                if val > dp[j][g] + 1e-9:
                    dp[j][g] = val
                    prev[j][g] = (i, g, 0, j)
                if g < gamma and dev[i] > 0:
                    val2 = base + p[i] + dev[i]
                    if val2 > dp[j][g + 1] + 1e-9:
                        dp[j][g + 1] = val2
                        prev[j][g + 1] = (i, g, 1, j)
    best_g = max(range(gamma + 1), key=lambda gg: dp[data.sink][gg])
    best_val = dp[data.sink][best_g]
    if best_val <= neg / 2:
        return None
    # Backtrack.
    path_rev = [data.sink]
    dev_nodes: List[int] = []
    cur, g = data.sink, best_g
    while cur != data.source:
        pr = prev[cur][g]
        if pr is None:
            return None
        i, g0, used_dev, _j = pr
        if used_dev:
            dev_nodes.append(i)
        path_rev.append(i)
        cur, g = i, g0
    path = list(reversed(path_rev))
    path_edges = [(path[t], path[t + 1]) for t in range(len(path) - 1)]
    return {
        "value": float(best_val),
        "path": path,
        "edges": tuple(path_edges),
        "used_budget": int(best_g),
        "deviation_nodes": tuple(sorted(dev_nodes)),
    }

def robust_cpm_lb_original(data: RCPSPData, gamma: int, alpha: float) -> float:
    sigma = {(i, j): 0 for i in range(data.n) for j in range(data.n) if i != j}
    for i in range(data.n):
        for j in data.successors[i]:
            sigma[i, j] = 1
    val = robust_longest_path_value(data, sigma, gamma, alpha)
    return 0.0 if val is None else val


def check_resource_flow_closure(data: RCPSPData, sigma: Dict[Tuple[int, int], int], tol: float = 1e-6) -> Dict[str, Any]:
    """Check resource-flow feasibility using the transitive closure of selected sigma arcs."""
    n = data.n
    source, sink = data.source, data.sink
    real = [i for i in range(n) if i not in (source, sink)]
    reach = transitive_closure_from_sigma(n, sigma)
    for k in data.renewable:
        cap = float(data.capacities[k])

        def req(i: int) -> float:
            return cap if i in (source, sink) else float(data.demands[i][k])

        S = 0
        L = lambda i: 1 + i
        R = lambda i: 1 + n + i
        T = 1 + 2 * n
        din = Dinic(T + 1)
        for i in [source] + real:
            din.add_edge(S, L(i), req(i))
        for j in real + [sink]:
            din.add_edge(R(j), T, req(j))
        for i in range(n):
            for j in range(n):
                if i != j and reach[i][j]:
                    coef = min(req(i), req(j))
                    if coef > 0:
                        din.add_edge(L(i), R(j), coef)
        required = cap + sum(req(i) for i in real)
        flow = din.max_flow(S, T)
        if flow < required - tol:
            return {"feasible": False, "resource": k, "flow": flow, "required": required, "reach": reach}
    return {"feasible": True}


def separate_exact_mfs(data: RCPSPData, sigma: Dict[Tuple[int, int], int], k: int) -> Optional[List[int]]:
    """Maximum-weight antichain + greedy reduction to an inclusion-minimal forbidden set."""
    n = data.n
    source, sink = data.source, data.sink
    real = [i for i in range(n) if i not in (source, sink) and data.demands[i][k] > 1e-9]
    if len(real) < 2:
        return None
    reach = transitive_closure_from_sigma(n, sigma)
    weights = {i: float(data.demands[i][k]) for i in real}
    total_w = sum(weights.values())
    cap = float(data.capacities[k])
    if total_w <= cap + 1e-9:
        return None

    S = 0
    L = lambda i: 1 + i
    R = lambda i: 1 + n + i
    T = 1 + 2 * n
    din = Dinic(T + 1)
    INF = total_w + cap + 1.0
    for i in real:
        din.add_edge(S, L(i), weights[i])
        din.add_edge(R(i), T, weights[i])
    for i in real:
        for j in real:
            if i != j and reach[i][j]:
                din.add_edge(L(i), R(j), INF)
    din.max_flow(S, T)
    U = din.reachable_from(S)
    A = [i for i in real if L(i) in U and R(i) not in U]
    if sum(weights[i] for i in A) <= cap + 1e-9:
        return None
    # Validate antichain.
    for a, i in enumerate(A):
        for j in A[a + 1:]:
            if reach[i][j] or reach[j][i]:
                return None
    # Inclusion-minimal reduction.
    F = sorted(A, key=lambda u: weights[u])
    changed = True
    while changed:
        changed = False
        for u in F[:]:
            if sum(weights[i] for i in F if i != u) > cap + 1e-9:
                F.remove(u)
                changed = True
    if len(F) < 2 or sum(weights[i] for i in F) <= cap + 1e-9:
        return None
    for a, i in enumerate(F):
        for j in F[a + 1:]:
            if reach[i][j] or reach[j][i]:
                return None
    return F


def serial_sgs_initial_solution(data: RCPSPData, gamma: int, alpha: float) -> Dict[str, Any]:
    n = data.n
    source, sink = data.source, data.sink
    real = [i for i in range(n) if i not in (source, sink)]
    preds = [set() for _ in range(n)]
    succ = [set() for _ in range(n)]
    for i in range(n):
        for j in data.successors[i]:
            succ[i].add(j)
            preds[j].add(i)
    closure = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in succ[i]:
            closure[i][j] = True
    for k in range(n):
        for i in range(n):
            if closure[i][k]:
                for j in range(n):
                    if closure[k][j]:
                        closure[i][j] = True
    succ_count = [sum(closure[i]) for i in range(n)]
    res_sum = [sum(data.demands[i][k] / max(1.0, data.capacities[k]) for k in data.renewable) for i in range(n)]
    rules = {
        "duration_desc": lambda i: (data.durations[i], succ_count[i], res_sum[i], -i),
        "duration_asc": lambda i: (-data.durations[i], succ_count[i], res_sum[i], -i),
        "resource_desc": lambda i: (res_sum[i], succ_count[i], data.durations[i], -i),
        "succ_desc": lambda i: (succ_count[i], res_sum[i], data.durations[i], -i),
        "id": lambda i: (-i,),
    }

    def run_rule(name: str, keyfun) -> Optional[Dict[str, Any]]:
        scheduled = {source}
        unscheduled = set(real)
        starts: Dict[int, float] = {source: 0.0}
        finish: Dict[int, float] = {source: 0.0}
        horizon = max(1, int(math.ceil(sum(data.durations[i] for i in real))))
        usage = {k: [0.0] * (horizon + 1) for k in data.renewable}
        while unscheduled:
            eligible = [i for i in unscheduled if all((pr == source) or (pr in scheduled) for pr in preds[i])]
            if not eligible:
                return None
            i = max(eligible, key=keyfun)
            est = max([int(finish.get(pr, 0)) for pr in preds[i]] + [0])
            pi = int(math.ceil(data.durations[i]))
            t = est
            while True:
                while t + pi >= len(next(iter(usage.values()))) if usage else False:
                    for kk in data.renewable:
                        usage[kk].extend([0.0] * max(1, horizon))
                    horizon *= 2
                ok = True
                for tau in range(t, t + pi):
                    for kk in data.renewable:
                        if usage[kk][tau] + data.demands[i][kk] > data.capacities[kk] + 1e-9:
                            ok = False
                            break
                    if not ok:
                        break
                if ok:
                    break
                t += 1
            starts[i] = float(t)
            finish[i] = float(t + pi)
            for tau in range(t, t + pi):
                for kk in data.renewable:
                    usage[kk][tau] += data.demands[i][kk]
            scheduled.add(i)
            unscheduled.remove(i)
        starts[sink] = max(finish[i] for i in real) if real else 0.0
        finish[sink] = starts[sink]
        sigma = {(i, j): 0 for i in range(n) for j in range(n) if i != j}
        for i in range(n):
            for j in data.successors[i]:
                sigma[i, j] = 1
        for i in range(n):
            for j in range(n):
                if i != j and finish.get(i, 0.0) <= starts.get(j, 0.0) + 1e-9:
                    sigma[i, j] = 1
        for j in real:
            sigma[source, j] = 1
            sigma[j, sink] = 1
            sigma[j, source] = 0
            sigma[sink, j] = 0
        sigma[source, sink] = 1
        sigma[sink, source] = 0
        val = robust_longest_path_value(data, sigma, gamma, alpha)
        if val is None:
            return None
        return {"rule": name, "sigma": sigma, "UB": val, "starts": starts}

    best = None
    for name, fun in rules.items():
        sol = run_rule(name, fun)
        if sol is not None and (best is None or sol["UB"] < best["UB"] - 1e-9):
            best = sol
    if best is not None:
        return best
    serial_ub = sum(data.durations[i] for i in real) + sum(sorted([alpha * data.durations[i] for i in real], reverse=True)[:gamma])
    return {"rule": "serial_fallback", "sigma": None, "UB": serial_ub, "starts": None}




def make_silent_model(name: str) -> gp.Model:
    """Create a Gurobi model without printing license/parameter messages."""
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


def build_hybrid_model(data: RCPSPData, initial_lb: float, initial_ub: float, initial_sigma: Optional[Dict[Tuple[int, int], int]]) -> Tuple[gp.Model, Dict[Tuple[int, int], gp.Var], gp.Var]:
    n = data.n
    V = list(range(n))
    source, sink = data.source, data.sink
    real = [i for i in V if i not in (source, sink)]
    arcs = [(i, j) for i in V for j in V if i != j]
    gamma = int(GAMMA)
    layers = list(range(gamma + 1))
    p = data.durations
    dev = [0.0 if i in (source, sink) else float(ALPHA) * p[i] for i in V]
    big_m = sum(p[i] + dev[i] for i in V) + 1.0

    m = make_silent_model("robust_rcpsp_lbbd_main")
    m.Params.TimeLimit = TIME_LIMIT
    m.Params.OutputFlag = GUROBI_OUTPUT
    if THREADS:
        m.Params.Threads = int(THREADS)
    try:
        m.Params.MIPFocus = int(MIP_FOCUS)
        m.Params.Heuristics = float(HEURISTICS_LEVEL)
        if NOREL_HEUR_TIME > 0:
            m.Params.NoRelHeurTime = float(NOREL_HEUR_TIME)
    except Exception:
        pass

    sigma = {a: m.addVar(vtype=GRB.BINARY, name=f"sig_{a[0]}_{a[1]}") for a in arcs}
    x = {(i, g): m.addVar(lb=0.0, name=f"x_{i}_{g}") for i in V for g in layers}
    eta = m.addVar(lb=max(0.0, initial_lb), name="eta")
    m.setObjective(eta, GRB.MINIMIZE)
    m.update()

    if initial_sigma is not None and SET_MIP_START_FROM_SGS:
        for key, val in initial_sigma.items():
            if key in sigma:
                sigma[key].Start = float(val)
        eta.Start = float(initial_ub)
    if ADD_ETA_CUTOFF_FROM_SGS and initial_ub < float("inf"):
        m.addConstr(eta <= float(initial_ub) + 1e-6, name="sgs_eta_cutoff")

    # Sequencing basics.
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
                    m.addConstr(sigma[i, j] >= sigma[i, k] + sigma[k, j] - 1, name=f"trans_{i}_{k}_{j}")

    if ADD_PAIRWISE_CONFLICTS:
        pair_cnt = 0
        for k in data.renewable:
            for a, i in enumerate(real):
                for j in real[a + 1:]:
                    if data.demands[i][k] + data.demands[j][k] > data.capacities[k] + 1e-9:
                        m.addConstr(sigma[i, j] + sigma[j, i] >= 1, name=f"pair_conf_{i}_{j}_k{k}")
                        pair_cnt += 1
        m._pair_cnt = pair_cnt
    else:
        m._pair_cnt = 0

    # Robust layered longest-path compact formulation.
    for g in layers:
        m.addConstr(x[source, g] == 0.0, name=f"source_layer_{g}")
        m.addConstr(eta >= x[sink, g], name=f"eta_sink_layer_{g}")
    for i, j in arcs:
        for g in layers:
            m.addConstr(x[j, g] >= x[i, g] + p[i] - big_m * (1 - sigma[i, j]), name=f"path_nom_{i}_{j}_{g}")
            if g < gamma:
                m.addConstr(x[j, g + 1] >= x[i, g] + p[i] + dev[i] - big_m * (1 - sigma[i, j]), name=f"path_dev_{i}_{j}_{g}")
    m.update()
    return m, sigma, eta


def _cb_get_sigma(model: gp.Model) -> Tuple[Dict[Tuple[int, int], int], Tuple[Tuple[int, int], ...]]:
    sigma_bar: Dict[Tuple[int, int], int] = {}
    selected: List[Tuple[int, int]] = []
    for key, var in model._sigma_vars.items():
        val = model.cbGetSolution(var)
        bit = 1 if val > 0.5 else 0
        sigma_bar[key] = bit
        if bit:
            selected.append(key)
    return sigma_bar, tuple(sorted(selected))


def _cb_lazy_mfs(model: gp.Model, F: List[int]) -> bool:
    sig = ("mfs", tuple(F))
    expr = gp.LinExpr()
    for a, i in enumerate(F):
        for j in F[a + 1:]:
            expr += model._sigma_vars[i, j] + model._sigma_vars[j, i]
    # Add even if repeated; Gurobi may revisit before synchronization.
    model.cbLazy(expr >= 1)
    if sig in model._lazy_signatures:
        model._stat["mfs_repeat"] += 1
        return False
    model._lazy_signatures.add(sig)
    model._stat["mfs"] += 1
    model._stat["mfs_size_sum"] += len(F)
    return True



def _path_cut_record(model: gp.Model, cert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    length = float(cert["value"])
    coeff = length - float(model._initial_lb)
    if coeff < PATH_CUT_MIN_COEFF:
        return None
    edges = tuple(cert["edges"])
    if not edges:
        return None
    return {"edges": edges, "length": length, "coeff": coeff}


def _path_cut_lhs_value(model: gp.Model, rec: Dict[str, Any], eta_val: float, sigma_vals: Dict[Tuple[int, int], float]) -> float:
    delta = 0.0
    for e in rec["edges"]:
        delta += 1.0 - float(sigma_vals[e])
    return float(eta_val) + float(rec["coeff"]) * delta


def _path_cut_expr(model: gp.Model, rec: Dict[str, Any]) -> gp.LinExpr:
    coeff = float(rec["coeff"])
    expr = gp.LinExpr()
    expr += model._eta_var
    for e in rec["edges"]:
        expr += coeff
        expr += -coeff * model._sigma_vars[e]
    return expr


def _add_path_cut_to_pool(model: gp.Model, rec: Dict[str, Any]) -> bool:
    sig = ("path", rec["edges"])
    if sig in model._path_cut_pool_sigs:
        model._stat["path_pool_repeat"] += 1
        return False
    if len(model._path_cut_pool) >= PATH_CUT_POOL_MAX_SIZE:
        # Drop the oldest cut. This is only a pool management decision; already
        # added Gurobi cuts remain in the model/tree.
        old = model._path_cut_pool.pop(0)
        model._path_cut_pool_sigs.discard(("path", old["edges"]))
    model._path_cut_pool.append(rec)
    model._path_cut_pool_sigs.add(sig)
    model._stat["path_pool"] += 1
    model._stat["path_len_sum"] += len(rec["edges"])
    return True


def _cb_check_path_pool_at_mipnode(model: gp.Model) -> None:
    if not getattr(model, "_use_path_cuts", False) or not PATH_CUT_CHECK_MIPNODE:
        return
    if not model._path_cut_pool:
        return
    try:
        status = model.cbGet(GRB.Callback.MIPNODE_STATUS)
        if status != GRB.OPTIMAL:
            return
        nodecnt = int(model.cbGet(GRB.Callback.MIPNODE_NODCNT))
        if PATH_CUT_MIPNODE_FREQ > 1 and nodecnt % int(PATH_CUT_MIPNODE_FREQ) != 0:
            return
        eta_rel = float(model.cbGetNodeRel(model._eta_var))
        added = 0
        scanned = 0
        # Recent cuts are usually more relevant.
        for rec in reversed(model._path_cut_pool):
            if scanned >= PATH_CUT_POOL_SCAN_LIMIT or added >= MAX_PATH_USER_CUTS_PER_NODE:
                break
            scanned += 1
            sig = ("path_user", rec["edges"])
            if sig in model._path_user_sigs:
                continue
            delta = 0.0
            for e in rec["edges"]:
                delta += 1.0 - float(model.cbGetNodeRel(model._sigma_vars[e]))
            lhs = eta_rel + float(rec["coeff"]) * delta
            viol = float(rec["length"]) - lhs
            if viol > PATH_CUT_VIOL_TOL:
                model.cbCut(_path_cut_expr(model, rec) >= float(rec["length"]))
                model._path_user_sigs.add(sig)
                model._stat["path_user"] += 1
                added += 1
        model._stat["path_pool_scans"] += scanned
    except Exception as exc:
        model._callback_error = "MIPNODE path cut error: " + repr(exc)
        model.terminate()

def _hybrid_callback(model: gp.Model, where: int) -> None:
    if where == GRB.Callback.MIP and getattr(model, "_progress_output", False):
        now = time.time()
        if now - getattr(model, "_last_progress_time", 0.0) >= model._progress_interval:
            try:
                node = model.cbGet(GRB.Callback.MIP_NODCNT)
                objbst = model.cbGet(GRB.Callback.MIP_OBJBST)
                objbnd = model.cbGet(GRB.Callback.MIP_OBJBND)
                solcnt = model.cbGet(GRB.Callback.MIP_SOLCNT)
                st = model._stat
                print(
                    f"\n[MIP] t={now-model._solve_start_time:7.1f}s node={node:.0f} sol={solcnt:.0f} "
                    f"objbst={objbst:.4g} objbnd={objbnd:.4g} "
                    f"SP={st.get('mipsol',0)} inf={st.get('inf',0)} feas={st.get('feas',0)} "
                    f"MFS={st.get('mfs',0)} rep={st.get('mfs_repeat',0)} miss={st.get('mfs_miss',0)} "
                    f"PCpool={st.get('path_pool',0)} PCuser={st.get('path_user',0)} "
                    f"NG={st.get('nogood',0)} cache={len(model._sp_cache)}/hit={st.get('cache_hit',0)}",
                    flush=True,
                )
                model._last_progress_time = now
            except Exception:
                pass
        return

    if where == GRB.Callback.MIPNODE:
        _cb_check_path_pool_at_mipnode(model)
        return

    if where != GRB.Callback.MIPSOL:
        return
    try:
        model._stat["mipsol"] += 1
        sigma_bar, key = _cb_get_sigma(model)
        if CACHE_SP_RESULTS and key in model._sp_cache:
            res = model._sp_cache[key]
            model._stat["cache_hit"] += 1
        else:
            res = check_resource_flow_closure(model._data, sigma_bar)
            if CACHE_SP_RESULTS:
                model._sp_cache[key] = res

        if res.get("feasible", False):
            model._stat["feas"] += 1
            # Eta is already enforced by the compact layered constraints for this
            # integer solution.  We nevertheless extract a robust critical path and
            # store the corresponding globally valid conditional path cut.  The cut
            # may not be violated at the current integer point, but it can cut future
            # fractional LP solutions at MIPNODE.
            if getattr(model, "_use_path_cuts", False) and PATH_CUT_FROM_SP1_FEASIBLE:
                cert = robust_longest_path_certificate(model._data, sigma_bar, int(model._gamma), float(model._alpha))
                if cert is not None:
                    rec = _path_cut_record(model, cert)
                    if rec is not None and _add_path_cut_to_pool(model, rec):
                        model._stat["path_cert"] += 1
                        # If numerical issues make the integer solution violate the
                        # path cut, add it lazily as an optimality cut.  Usually it is
                        # already satisfied because SP2 is embedded in the master.
                        if PATH_CUT_ADD_LAZY_IF_VIOLATED_AT_MIPSOL:
                            eta_sol = float(model.cbGetSolution(model._eta_var))
                            lhs = eta_sol  # all path arcs are selected for this certificate
                            if rec["length"] - lhs > PATH_CUT_VIOL_TOL:
                                model.cbLazy(_path_cut_expr(model, rec) >= float(rec["length"]))
                                model._stat["path_lazy"] += 1
                else:
                    model._stat["path_cert_fail"] += 1
            return

        model._stat["inf"] += 1
        added = 0
        k = int(res["resource"])
        F = separate_exact_mfs(model._data, sigma_bar, k) if ADD_MFS_CUTS else None
        if F is not None:
            _cb_lazy_mfs(model, F)
            added += 1
        else:
            model._stat["mfs_miss"] += 1

        # Last resort: full no-good should be rare. It preserves correctness but is weak.
        if added == 0 and USE_FULL_NOGOOD_FALLBACK:
            sig = ("nogood", key)
            if sig not in model._lazy_signatures:
                expr = gp.LinExpr()
                for a, val in sigma_bar.items():
                    expr += (1 - model._sigma_vars[a]) if val == 1 else model._sigma_vars[a]
                model.cbLazy(expr >= 1)
                model._lazy_signatures.add(sig)
                model._stat["nogood"] += 1
    except Exception as exc:
        model._callback_error = repr(exc)
        model.terminate()


def solve(data: RCPSPData) -> None:
    gamma = int(GAMMA)
    if gamma < 0 or gamma != GAMMA:
        raise ValueError("GAMMA must be a nonnegative integer.")
    n = data.n
    real = [i for i in range(n) if i not in (data.source, data.sink)]
    initial_lb = robust_cpm_lb_original(data, gamma, ALPHA)
    sgs = serial_sgs_initial_solution(data, gamma, ALPHA) if USE_INITIAL_SGS_UB else {"rule": "none", "sigma": None, "UB": float("inf")}
    initial_ub = float(sgs.get("UB", float("inf")))
    initial_sigma = sgs.get("sigma") if isinstance(sgs.get("sigma"), dict) else None

    print("========== LBBD-main: layered optimality + MFS + path cuts ==========")
    print(f"file              : {FILE_PATH}")
    print(f"activities        : {n} real={len(real)}")
    print(f"resources         : total={len(data.capacities)} renewable={data.renewable}")
    print(f"Gamma             : {gamma}")
    print(f"alpha             : {ALPHA}")
    print(f"initial CPM LB    : {initial_lb:.6g}")
    print(f"initial SGS UB    : {initial_ub:.6g}  rule={sgs.get('rule','none')}")
    print(f"transitivity      : {ADD_TRANSITIVITY}")
    print(f"pairwise conflicts: {ADD_PAIRWISE_CONFLICTS}")
    print(f"MFS lazy cuts     : {ADD_MFS_CUTS}  nogoodFallback={USE_FULL_NOGOOD_FALLBACK}")
    print(f"path opt cuts     : {ADD_PATH_OPT_CUTS}  mipnode={PATH_CUT_CHECK_MIPNODE}")
    print(f"path pool limits  : max={PATH_CUT_POOL_MAX_SIZE} scan={PATH_CUT_POOL_SCAN_LIMIT} perNode={MAX_PATH_USER_CUTS_PER_NODE}")
    print("NOTE: SP2 is compact in MP; SP1 infeasible -> MFS; SP1 feasible -> path-cut pool.")
    print("===================================================================")

    t0 = time.time()
    m, sigma, eta = build_hybrid_model(data, initial_lb, initial_ub, initial_sigma)
    build_time = time.time() - t0
    print(f"Model built        : {build_time:.2f}s vars={m.NumVars} constrs={m.NumConstrs} pairCuts={getattr(m,'_pair_cnt',0)}")

    m.Params.LazyConstraints = 1
    m.Params.PreCrush = 1
    m._data = data
    m._sigma_vars = sigma
    m._eta_var = eta
    m._gamma = gamma
    m._alpha = ALPHA
    m._initial_lb = initial_lb
    m._use_path_cuts = ADD_PATH_OPT_CUTS
    m._sp_cache: Dict[Tuple[Tuple[int, int], ...], Dict[str, Any]] = {}
    m._lazy_signatures: Set[Tuple[Any, ...]] = set()
    m._path_cut_pool: List[Dict[str, Any]] = []
    m._path_cut_pool_sigs: Set[Tuple[Any, ...]] = set()
    m._path_user_sigs: Set[Tuple[Any, ...]] = set()
    m._stat = {
        "mipsol": 0, "inf": 0, "feas": 0,
        "mfs": 0, "mfs_repeat": 0, "mfs_miss": 0, "mfs_size_sum": 0,
        "nogood": 0, "cache_hit": 0,
        "path_cert": 0, "path_cert_fail": 0, "path_pool": 0, "path_pool_repeat": 0,
        "path_len_sum": 0, "path_user": 0, "path_lazy": 0, "path_pool_scans": 0,
    }
    m._callback_error = None
    m._progress_output = PROGRESS_OUTPUT
    m._progress_interval = PROGRESS_INTERVAL_SEC
    m._last_progress_time = 0.0
    m._solve_start_time = time.time()

    m.optimize(_hybrid_callback)
    total_time = time.time() - t0
    if m._callback_error:
        print(f"callback error: {m._callback_error}")

    print("\n========== Hybrid v17 result ==========")
    print(f"status       : {m.Status}")
    print(f"build time   : {build_time:.2f}s")
    print(f"total time   : {total_time:.2f}s")
    print(f"vars/constrs : {m.NumVars}/{m.NumConstrs}")
    print(f"stats        : {m._stat}")
    print(f"lazy cuts    : {len(m._lazy_signatures)}")
    print(f"path pool    : {len(getattr(m, '_path_cut_pool', []))}  userCuts={len(getattr(m, '_path_user_sigs', []))}")
    print(f"cache size   : {len(m._sp_cache)}")
    if m.SolCount > 0:
        gap = (m.ObjVal - m.ObjBound) / max(1.0, abs(m.ObjVal))
        print(f"objective    : {m.ObjVal:.6g}")
        print(f"best bound   : {m.ObjBound:.6g}")
        print(f"gap          : {100*gap:.4f}%")
        # Optional verification of incumbent sigma.
        sigma_sol = {a: 1 if var.X > 0.5 else 0 for a, var in sigma.items()}
        val = robust_longest_path_value(data, sigma_sol, gamma, ALPHA)
        res = check_resource_flow_closure(data, sigma_sol)
        print(f"verify Trob  : {val}")
        print(f"verify res   : {res.get('feasible', False)}")
    else:
        print("No feasible solution found.")
    print("=======================================")


def main() -> None:
    data = load_psplib_sm(FILE_PATH)
    solve(data)


if __name__ == "__main__":
    main()
