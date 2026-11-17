"""Read-only local corpus inspection. Excerpts are opt-in and never sent to a model."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from interview_copilot.services.corpus import CorpusIndex, source_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--label", default="notes")
    parser.add_argument("--sources-file", type=Path)
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--show-excerpts", action="store_true")
    args = parser.parse_args()
    if args.root and args.sources_file:
        parser.error("select a root or a source manifest")
    sources = ([{"label": args.label, "path": str(args.root)}] if args.root else
               source_settings({"corpus": {"sources_file": str(args.sources_file)}} if args.sources_file else {}, ROOT))
    index = CorpusIndex()
    try:
        index.sync_sources(sources)
        results = {}
        for question in args.query:
            results[question] = [{key: value for key, value in item.items()
                                  if args.show_excerpts or key != "excerpt"}
                                 for item in index.search(question)]
        print(json.dumps({"status": index.stats(), "queries": results}, ensure_ascii=True))
    finally:
        index.close()


if __name__ == "__main__":
    main()
