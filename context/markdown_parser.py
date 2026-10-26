"""One-shot native Markdown parser, supervised by lossless_tree's timeout."""
import json
import sys

from .lossless_tree import _build_lossless_tree


def main() -> None:
    source = sys.stdin.read()
    artifact = _build_lossless_tree("source.md", source, isolate_markdown=False)
    json.dump(artifact["tree"], sys.stdout)


if __name__ == "__main__":
    main()
