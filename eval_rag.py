"""
eval_rag.py — retrieval quality eval for the RAG layer (Phase 1.2 acceptance).

Reads eval cases from eval_queries.json:
    [
      {"query": "when did we talk about the broken dishwasher",
       "expect": ["dishwasher"],            # substrings; a hit counts if ANY appears
       "channel": "general"}                # optional: hit must come from this channel
    ]

For each case, runs the search and reports whether an expected chunk appears in
the top 5 / top 20 results (ignoring the distance threshold), plus the distance
of the first correct hit — so thresholds can be tuned from real data.

Compare two models/collections:
    venv/bin/python eval_rag.py \
        --run BAAI/bge-small-en-v1.5:discord_chunks \
        --run all-MiniLM-L6-v2:discord_chunks_minilm
"""

import argparse
import json
import os
import sys

import chromadb
from sentence_transformers import SentenceTransformer

CHROMADB_HOST = os.getenv("CHROMADB_HOST", "localhost")
CHROMADB_PORT = int(os.getenv("CHROMADB_PORT", "8100"))


def query_prefix(model_name: str) -> str:
    return ("Represent this sentence for searching relevant passages: "
            if "bge" in model_name else "")


def run_eval(model_name: str, collection_name: str, cases: list) -> dict:
    model = SentenceTransformer(model_name)
    client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    collection = client.get_collection(collection_name)
    prefix = query_prefix(model_name)

    results = []
    for case in cases:
        emb = model.encode(prefix + case["query"], normalize_embeddings=True).tolist()
        res = collection.query(
            query_embeddings=[emb], n_results=20,
            include=["documents", "metadatas", "distances"],
        )
        docs  = res["documents"][0]
        metas = res["metadatas"][0]
        dists = res["distances"][0]

        rank, dist = None, None
        for i, (doc, meta) in enumerate(zip(docs, metas)):
            if case.get("channel") and meta.get("channel") != case["channel"]:
                continue
            text = doc.lower()
            if any(k.lower() in text for k in case["expect"]):
                rank, dist = i + 1, dists[i]
                break
        results.append({
            "query": case["query"], "rank": rank, "dist": dist,
            "top1_dist": dists[0] if dists else None,
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
