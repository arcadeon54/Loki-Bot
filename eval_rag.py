"""
eval_rag.py — retrieval quality eval for the RAG layer (Phase 1.2 acceptance).

Runs the PRODUCTION search path (rag_search.search_history — message-level
matching expanded to chunks) with the distance threshold disabled, so it
reports true ranks and the distances needed to tune RAG_MAX_DISTANCE.

Reads eval cases from eval_queries.json:
    [
      {"query": "when did we talk about the broken dishwasher",
       "expect": ["dishwasher"],            # substrings; a hit counts if ANY appears
       "channel": "general"}                # optional: hit must come from this channel
    ]

Compare two models/collections:
    venv/bin/python eval_rag.py \
        --run BAAI/bge-small-en-v1.5:discord_chunks \
        --run all-MiniLM-L6-v2:discord_chunks_minilm
"""

import argparse
import json
import sys

import rag_search


def configure(model_name: str, collection_name: str):
    """Point rag_search at a model/collection pair and reset its singletons."""
    rag_search.EMBED_MODEL = model_name
    rag_search.QUERY_PREFIX = (
        "Represent this sentence for searching relevant passages: "
        if "bge" in model_name else ""
    )
    rag_search.COLLECTION_NAME = collection_name
    rag_search.MAX_DISTANCE = 9.0            # disabled: eval wants raw ranks
    rag_search.MAX_DISTANCE_WINDOWED = 9.0
    rag_search._model = None
    rag_search._client = None
    rag_search._chunk_collection = None
    rag_search._msg_collection = None


def run_eval(model_name: str, collection_name: str, cases: list) -> dict:
    configure(model_name, collection_name)
    results = []
    for case in cases:
        hits = rag_search.search_history(case["query"], n_results=20)
        hits.sort(key=lambda h: h["distance"])   # rank by relevance, not chronology

        rank, dist = None, None
        for i, h in enumerate(hits):
            if case.get("channel") and h["channel"] != case["channel"]:
                continue
            text = h["text"].lower()
            if any(k.lower() in text for k in case["expect"]):
                rank, dist = i + 1, h["distance"]
                break
        results.append({
            "query": case["query"], "rank": rank, "dist": dist,
            "top1_dist": hits[0]["distance"] if hits else None,
        })

    hit5  = sum(1 for r in results if r["rank"] and r["rank"] <= 5)
    hit20 = sum(1 for r in results if r["rank"])
    return {"model": model_name, "collection": collection_name,
            "results": results, "hit5": hit5, "hit20": hit20, "n": len(results)}


def report(run: dict):
    print(f"\n=== {run['model']}  (collection: {run['collection']}) ===")
    print(f"hit@5: {run['hit5']}/{run['n']}   hit@20: {run['hit20']}/{run['n']}")
    print(f"{'rank':>4}  {'dist':>6}  {'top1':>6}  query")
    for r in run["results"]:
        rank = r["rank"] if r["rank"] else "MISS"
        dist = f"{r['dist']:.3f}" if r["dist"] is not None else "—"
        top1 = f"{r['top1_dist']:.3f}" if r["top1_dist"] is not None else "—"
        print(f"{rank:>4}  {dist:>6}  {top1:>6}  {r['query'][:70]}")
    found = [r["dist"] for r in run["results"] if r["dist"] is not None]
    if found:
        print(f"correct-hit distances: min {min(found):.3f}  max {max(found):.3f}  "
              f"mean {sum(found)/len(found):.3f}")
        print(f"→ suggested RAG_MAX_DISTANCE ≥ {max(found):.2f} (covers all correct hits)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default="eval_queries.json")
    ap.add_argument("--run", action="append", required=True,
                    metavar="MODEL:COLLECTION",
                    help="model and collection to evaluate, e.g. BAAI/bge-small-en-v1.5:discord_chunks")
    args = ap.parse_args()

    with open(args.queries) as f:
        cases = json.load(f)
    if not cases:
        sys.exit("eval_queries.json is empty")

    for spec in args.run:
        model_name, _, coll = spec.rpartition(":")
        if not model_name:
            sys.exit(f"bad --run spec: {spec} (want MODEL:COLLECTION)")
        report(run_eval(model_name, coll, cases))
