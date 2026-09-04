"""Freeze compact schema/instructions/taxonomy prefix without calling Luna."""
import argparse
import json
from pathlib import Path

from image_rag_eval.luna_compact import prepare_contract

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare_contract(Path(__file__).resolve().parents[1], apply=args.apply), ensure_ascii=False))
