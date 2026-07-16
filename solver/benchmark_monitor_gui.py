# -*- coding: utf-8 -*-
"""
External Tkinter monitor for robust RCPSP benchmark runners.

Usage:
    python benchmark_monitor_gui.py path/to/live_status.json

The runner writes a small JSON status file. This monitor reads it periodically
and updates a stable popup window. It is intentionally read-only and does not
start/stop Gurobi, so it has negligible impact on the benchmark process.
"""
from __future__ import annotations

import json
import os
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def fmt_seconds(sec):
    try:
        if sec is None:
            return "--"
        sec = max(0, float(sec))
    except Exception:
        return "--"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_num(x, digits=3):
    try:
        if x is None or x == "":
            return "--"
        y = float(x)
        if abs(y) >= 1000:
            return f"{y:,.1f}"
        return f"{y:.{digits}f}"
    except Exception:
        return "--"


def fmt_gap(x):
    try:
        if x is None or x == "":
            return "--"
        return f"{100*float(x):.2f}%"
    except Exception:
        return "--"


def safe_name(path):
    try:
        return Path(str(path)).name
    except Exception:
        return str(path or "--")


class MonitorApp:
    def __init__(self, root: tk.Tk, status_path: str):
        self.root = root
        self.status_path = status_path
        self.frame = 0
        self.last_payload = {}
        self.root.title("Robust RCPSP Benchmark Monitor")
        self.root.geometry("1160x760")
        self.root.minsize(1040, 700)
        self.root.configure(bg="#f6f8fb")
        self._build_ui()
        self._tick()

    def _label(self, parent, text="", font=None, fg="#1f2937", bg="#f6f8fb", anchor="w"):
        lab = tk.Label(parent, text=text, font=font or ("Microsoft YaHei UI", 10), fg=fg, bg=bg, anchor=anchor)
        return lab

    def _build_ui(self):
        title_frame = tk.Frame(self.root, bg="#1f4e78", padx=14, pady=10)
        title_frame.pack(fill="x")
        self.title_var = tk.StringVar(value="⠋ Robust RCPSP Benchmark Monitor")
        tk.Label(title_frame, textvariable=self.title_var, font=("Microsoft YaHei UI", 18, "bold"), fg="white", bg="#1f4e78", anchor="w").pack(fill="x")
        self.subtitle_var = tk.StringVar(value=f"Watching: {self.status_path}")
        tk.Label(title_frame, textvariable=self.subtitle_var, font=("Consolas", 10), fg="#dbeafe", bg="#1f4e78", anchor="w").pack(fill="x", pady=(4,0))

        main = tk.Frame(self.root, bg="#f6f8fb", padx=14, pady=12)
        main.pack(fill="both", expand=True)

        # KPI row
        kpi = tk.Frame(main, bg="#f6f8fb")
        kpi.pack(fill="x")
        self.kpi_vars = {}
        for key, label in [("done", "Done"), ("elapsed", "Elapsed"), ("eta", "ETA mean"), ("status", "Gap / Status")]:
            card = tk.Frame(kpi, bg="white", bd=1, relief="solid", padx=12, pady=8)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            tk.Label(card, text=label, bg="white", fg="#6b7280", font=("Microsoft YaHei UI", 11)).pack(anchor="w")
            var = tk.StringVar(value="--")
            self.kpi_vars[key] = var
            tk.Label(card, textvariable=var, bg="white", fg="#111827", font=("Consolas", 18, "bold")).pack(anchor="w", pady=(2,0))

        # Progress section
        prog = tk.LabelFrame(main, text="Progress", bg="#f6f8fb", fg="#1f2937", font=("Microsoft YaHei UI", 12, "bold"), padx=12, pady=10)
        prog.pack(fill="x", pady=(12, 8))
        self.overall_var = tk.StringVar(value="Overall progress: --")
        tk.Label(prog, textvariable=self.overall_var, bg="#f6f8fb", anchor="w", font=("Microsoft YaHei UI", 12)).pack(fill="x")
        self.overall_pb = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.overall_pb.pack(fill="x", pady=(4, 10))
        self.current_var = tk.StringVar(value="Current gap progress: --")
        tk.Label(prog, textvariable=self.current_var, bg="#f6f8fb", anchor="w", font=("Microsoft YaHei UI", 12)).pack(fill="x")
        self.current_pb = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.current_pb.pack(fill="x", pady=(4, 6))
        self.runtime_var = tk.StringVar(value="Runtime: --")
        tk.Label(prog, textvariable=self.runtime_var, bg="#f6f8fb", anchor="w", font=("Microsoft YaHei UI", 12)).pack(fill="x")

        # Current job details
        cur = tk.LabelFrame(main, text="Current job", bg="#f6f8fb", fg="#1f2937", font=("Microsoft YaHei UI", 12, "bold"), padx=12, pady=10)
        cur.pack(fill="x", pady=(8, 8))
        self.current_detail = tk.StringVar(value="--")
        tk.Label(cur, textvariable=self.current_detail, bg="#f6f8fb", justify="left", anchor="w", font=("Consolas", 13, "bold")).pack(fill="x")

        # Two tables: recent and slowest
        tables = tk.Frame(main, bg="#f6f8fb")
        tables.pack(fill="both", expand=True)
        self.recent_text = tk.Text(tables, height=10, bg="white", fg="#111827", font=("Consolas", 11), bd=1, relief="solid")
        self.slow_text = tk.Text(tables, height=10, bg="white", fg="#111827", font=("Consolas", 11), bd=1, relief="solid")
        left = tk.Frame(tables, bg="#f6f8fb")
        right = tk.Frame(tables, bg="#f6f8fb")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        tk.Label(left, text="Recent completed", bg="#f6f8fb", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self.recent_text.pack(fill="both", expand=True)
        tk.Label(right, text="Slowest finished so far", bg="#f6f8fb", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        self.slow_text.pack(fill="both", expand=True)

        bottom = tk.Frame(self.root, bg="#e5e7eb", padx=12, pady=5)
        bottom.pack(fill="x")
        self.footer_var = tk.StringVar(value="Waiting for runner...")
        tk.Label(bottom, textvariable=self.footer_var, bg="#e5e7eb", fg="#374151", font=("Consolas", 10), anchor="w").pack(fill="x")

    def _load_payload(self):
        try:
            with open(self.status_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_text(self, widget: tk.Text, lines):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(lines) if lines else "--")
        widget.configure(state="disabled")

    def _tick(self):
        self.frame += 1
        payload = self._load_payload()
        if payload:
            self.last_payload = payload
            self._render(payload)
        else:
            spin = SPINNER[self.frame % len(SPINNER)]
            self.title_var.set(f"{spin} Robust RCPSP Benchmark Monitor")
            self.footer_var.set(f"Waiting for status file: {self.status_path}")
        self.root.after(500, self._tick)  # 0.5s refresh as requested

    def _render(self, payload):
        spin = SPINNER[self.frame % len(SPINNER)]
        ri = payload.get("run_info", {})
        prog = payload.get("progress", {})
        cur = payload.get("current", {}) or {}
        total = int(prog.get("total_jobs") or ri.get("total_jobs") or 0)
        done = int(prog.get("done_jobs") or 0)
        ratio = 100 * done / total if total else 0
        cur_elapsed = float(cur.get("elapsed") or cur.get("runtime_total") or 0)
        tl = float(cur.get("time_limit") or ri.get("time_limit") or 1)
        time_ratio = max(0, min(100, 100 * cur_elapsed / tl)) if tl else 0
        raw_gap = cur.get("gap")
        gap_known = False
        gap_value = None
        try:
            if raw_gap is not None and raw_gap != "":
                gap_value = float(raw_gap)
                gap_known = True
        except Exception:
            gap_known = False
        if str(cur.get("status_name") or "").upper() == "OPTIMAL":
            gap_known = True
            gap_value = 0.0
        if gap_known:
            gap_pct = max(0.0, gap_value or 0.0)
            # Gap is stored as a fraction. A 0% gap means proof complete; 100%+ gap means no proof progress.
            gap_progress = max(0.0, min(100.0, 100.0 * (1.0 - min(gap_pct, 1.0))))
        else:
            gap_progress = 0.0
        method = ri.get("method", "--")
        dataset = ri.get("dataset", "--")
        self.title_var.set(f"{spin} Robust RCPSP Benchmark Monitor   [{method}]   {done}/{total}")
        self.subtitle_var.set(f"Dataset={dataset} | Gamma={ri.get('gamma_list')} | alpha={ri.get('alpha_list')} | Output={ri.get('output_path')}")
        self.kpi_vars["done"].set(f"{done}/{total} ({ratio:.1f}%)")
        self.kpi_vars["elapsed"].set(fmt_seconds(prog.get("elapsed")))
        self.kpi_vars["eta"].set(fmt_seconds(prog.get("eta_mean")))
        status_txt = str(cur.get("status_name") or "RUNNING" if cur else "--")
        self.kpi_vars["status"].set(f"{fmt_gap(gap_value) if gap_known else '--'}  {status_txt}")
        self.overall_var.set(f"Overall progress: {done}/{total} = {ratio:.1f}%")
        self.overall_pb["value"] = ratio
        if gap_known:
            self.current_var.set(f"Current gap progress: {gap_progress:.1f}% complete    gap={fmt_gap(gap_value)}")
        else:
            self.current_var.set("Current gap progress: waiting for incumbent/bound")
        self.current_pb["value"] = gap_progress
        self.runtime_var.set(f"Runtime: {fmt_seconds(cur_elapsed)} / {fmt_seconds(tl)} = {time_ratio:.1f}% of time limit")
        file_name = safe_name(cur.get("file")) if cur else "--"
        detail = (
            f"job          : {prog.get('current_index','--')}/{total}  {file_name}\n"
            f"method       : {cur.get('method', method)}\n"
            f"Gamma/alpha  : {cur.get('gamma','--')} / {cur.get('alpha','--')}\n"
            f"obj/bound/gap: {fmt_num(cur.get('objective'))} / {fmt_num(cur.get('best_bound'))} / {fmt_gap(cur.get('gap'))}\n"
            f"runtime/TL   : {fmt_seconds(cur_elapsed)} / {fmt_seconds(tl)}  ({time_ratio:.1f}%)\n"
            f"nodes/sol    : {fmt_num(cur.get('nodes'),0)} / {cur.get('sol_count','--')}\n"
            f"SP/MFS/path  : {cur.get('sp_calls','--')} / {cur.get('mfs_cuts','--')} / {cur.get('path_user',cur.get('path_cuts','--'))}"
        )
        self.current_detail.set(detail)
        recent_lines = []
        for r in payload.get("recent", [])[:10]:
            recent_lines.append(f"{safe_name(r.get('file')):<12} G={r.get('gamma')} a={r.get('alpha')} {str(r.get('status_name')):<10} time={fmt_seconds(r.get('runtime_total') or r.get('total_time')):>8} gap={fmt_gap(r.get('gap')):>8} obj={fmt_num(r.get('objective'))}")
        self._write_text(self.recent_text, recent_lines)
        hard_lines = []
        for r in payload.get("hard", [])[:8]:
            hard_lines.append(f"{safe_name(r.get('file')):<12} G={r.get('gamma')} a={r.get('alpha')} {str(r.get('status_name')):<10} time={fmt_seconds(r.get('runtime_total') or r.get('total_time')):>8} gap={fmt_gap(r.get('gap')):>8}")
        self._write_text(self.slow_text, hard_lines)
        try:
            age = time.time() - os.path.getmtime(self.status_path)
            self.footer_var.set(f"Last update: {payload.get('timestamp','--')} | status age={age:.1f}s | JSON={self.status_path}")
        except Exception:
            self.footer_var.set(f"Last update: {payload.get('timestamp','--')} | JSON={self.status_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python benchmark_monitor_gui.py path/to/live_status.json")
        return
    path = sys.argv[1]
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        style.theme_use("clam")
    except Exception:
        pass
    MonitorApp(root, path)
    root.mainloop()


if __name__ == "__main__":
    main()
