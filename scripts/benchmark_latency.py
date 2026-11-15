"""Benchmark SwiftPay API latency for cache misses and exact cache hits."""
import json
import statistics
import time
import uuid
from pathlib import Path

from scripts.evaluate import ask

OUTPUT = Path("data/latency_report.json")
SAMPLES = 20

# Use distinct question text for each cold request to avoid exact-cache reuse.
# Cache status is checked; a semantic hit is NOT counted as an uncached response.
BASE_QUESTION = "How soon must a P1 incident be acknowledged?"


def percentile(values, p):
    values = sorted(values)
    position = (len(values) - 1) * p / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(values):
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "mean_ms": None}
    return {
        "count": len(values),
        "p50_ms": round(percentile(values, 50), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "mean_ms": round(statistics.mean(values), 1),
    }


def main():
    results = {"uncached": [], "exact_cache": [], "other": []}
    run_id = uuid.uuid4().hex[:10]

    for i in range(SAMPLES):
        question = (
            f"{BASE_QUESTION} "
            f"Benchmark reference {run_id}-{i}: answer using the incident policy."
        )

        first = ask("employee", None, question)
        first_status = first.get("cache", {}).get("status", "unknown")
        first_ms = first["_ms"]

        if first_status in ("miss", "off"):
            results["uncached"].append(first_ms)
        else:
            results["other"].append({
                "sample": i + 1,
                "phase": "first",
                "status": first_status,
                "latency_ms": round(first_ms, 1),
            })

        second = ask("employee", None, question)
        second_status = second.get("cache", {}).get("status", "unknown")
        second_ms = second["_ms"]

        if second_status == "exact":
            results["exact_cache"].append(second_ms)
        else:
            results["other"].append({
                "sample": i + 1,
                "phase": "repeat",
                "status": second_status,
                "latency_ms": round(second_ms, 1),
            })

        print(
            f"{i+1:02}/{SAMPLES} "
            f"first={first_ms:.0f}ms ({first_status}) "
            f"repeat={second_ms:.0f}ms ({second_status})"
        )

        # Space request pairs to stay below the default 30/minute limit.
        # Each iteration sends two requests.
        time.sleep(5)

    report = {
        "samples_requested": SAMPLES,
        "uncached": summarize(results["uncached"]),
        "exact_cache": summarize(results["exact_cache"]),
        "other": results["other"],
        "limitations": [
            "End-to-end API client latency includes HTTP and authentication overhead.",
            "Unique benchmark suffixes may affect retrieval and answer generation.",
            "A cache miss is not necessarily a successful generated answer.",
            "Runs are sequential and do not measure concurrent load.",
            "The first-request group excludes semantic cache hits.",
        ],
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\nLATENCY SUMMARY")
    print(json.dumps({
        "uncached": report["uncached"],
        "exact_cache": report["exact_cache"],
        "other_count": len(report["other"]),
    }, indent=2))
    print(f"\nSaved: {OUTPUT}")


if __name__ == "__main__":
    main()
