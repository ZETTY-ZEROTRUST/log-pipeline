#!/usr/bin/env python3
"""contracts/MANIFEST.json 생성·검사.

Java/Python consumer가 같은 schema·fixture revision을 고정(pin)하도록 각 파일의
sha256과 전체 revision hash를 기록한다.

- 대상: contracts/<name>/<version>/ 아래의 모든 *.json (schema, fixture, index).
- 파일 hash: 저장소에 커밋된 바이트 그대로의 sha256 (contracts/.gitattributes 가 LF를 고정).
- revision: 경로 오름차순으로 "<posix 경로> <sha256 hex>\\n" 을 이어 붙인 UTF-8의 sha256.

사용법:
  hash.py            현재 계산값 출력
  hash.py --write    MANIFEST.json 갱신
  hash.py --check    MANIFEST.json 과 현재 파일이 다르면 exit 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = CONTRACTS_DIR / "MANIFEST.json"
CONTRACT_DIRS = ("security-event/v2", "anomaly-detection/v1", "response-command/v1")

SCHEMAS = {
    "security-event/2.0": "security-event/v2/schema.json",
    "anomaly-detection/1.0": "anomaly-detection/v1/schema.json",
    "response-command/1.0": "response-command/v1/schema.json",
    "response-result/1.0": "response-command/v1/result.schema.json",
}


def contract_files() -> List[Path]:
    files: List[Path] = []
    for rel in CONTRACT_DIRS:
        files.extend(p for p in (CONTRACTS_DIR / rel).rglob("*.json") if p.is_file())
    return sorted(files, key=lambda p: p.relative_to(CONTRACTS_DIR).as_posix())


def build_manifest() -> Dict[str, object]:
    digests: Dict[str, str] = {}
    for path in contract_files():
        digests[path.relative_to(CONTRACTS_DIR).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    revision_input = "".join("%s %s\n" % (rel, digest) for rel, digest in digests.items())
    return {
        "manifest_version": 1,
        "algorithm": "sha256",
        "revision": "sha256:" + hashlib.sha256(revision_input.encode("utf-8")).hexdigest(),
        "schemas": {version: {"path": rel, "sha256": digests[rel]} for version, rel in SCHEMAS.items()},
        "files": digests,
    }


def render(manifest: Dict[str, object]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="contracts/MANIFEST.json 생성·검사")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="MANIFEST.json 갱신")
    group.add_argument("--check", action="store_true", help="MANIFEST.json 이 현재 파일과 일치하는지 검사")
    args = parser.parse_args(argv)

    text = render(build_manifest())
    if args.write:
        MANIFEST_PATH.write_text(text, encoding="utf-8")
        print("wrote %s" % MANIFEST_PATH.relative_to(CONTRACTS_DIR.parent).as_posix())
        return 0
    if args.check:
        current = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else ""
        if current != text:
            print("MANIFEST.json is stale; run: python contracts/tools/hash.py --write")
            return 1
        print("MANIFEST.json OK (%s)" % json.loads(text)["revision"])
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
