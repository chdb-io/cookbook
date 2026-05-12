"""3-run median benchmark of the aligned workload (Q1-Q18 in §2 order)."""

import json, os, platform, statistics, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RUNS = 3


def run(script):
    # Use Popen so we can tolerate a non-zero exit code on shutdown noise
    # (chDB 4.1.6 occasionally hits a benign mutex error during Session destruction
    # AFTER all queries have completed and __RESULTS_JSON__ has been printed).
    p = subprocess.Popen([PY, os.path.join(HERE, script)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    print(out, end="")
    for line in out.splitlines():
        if line.startswith("__RESULTS_JSON__"):
            return json.loads(line.split(" ", 1)[1])
    raise RuntimeError(f"no results line in {script} (exit code {p.returncode})")


def aggregate(runs):
    per = {}
    for r in runs:
        for q in r["queries"]:
            per.setdefault(q["name"], []).append(q["elapsed_ms"])
    return [{"name": k, "median_ms": statistics.median(v),
             "min_ms": min(v), "max_ms": max(v)} for k, v in per.items()]


def main():
    duck = [run("workload_aligned_duckdb.py") for _ in range(RUNS)]
    chdb = [run("workload_aligned_chdb.py") for _ in range(RUNS)]
    d, c = aggregate(duck), aggregate(chdb)

    print("\n========== ALIGNED COMPARISON (median of 3 runs, §2 order) ==========")
    print(f"{'Query':<46} {'DuckDB':>12} {'chDB':>12} {'speedup':>10}")
    print("-" * 84)
    rows = []
    for di, ci in zip(d, c):
        spd = di["median_ms"] / ci["median_ms"]
        marker = "  ← chDB" if spd > 1.1 else ("  ← DuckDB" if spd < 0.9 else "")
        print(f"{di['name']:<46} {di['median_ms']:>10.1f} ms {ci['median_ms']:>10.1f} ms {spd:>9.2f}x{marker}")
        rows.append({"name": di["name"], "duckdb_ms": di["median_ms"],
                     "chdb_ms": ci["median_ms"], "speedup": spd})

    report = {
        "machine": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "engines": {"duckdb_version": duck[0]["version"], "chdb_version": chdb[0]["version"]},
        "runs_per_engine": RUNS,
        "comparison": rows,
    }
    with open(os.path.join(HERE, "results_aligned.json"), "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nWrote {os.path.join(HERE, 'results_aligned.json')}")


if __name__ == "__main__":
    main()
