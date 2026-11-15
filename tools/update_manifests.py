"""Explicitly regenerate source integrity manifests after reviewing a change."""
import hashlib
import json
from pathlib import Path

from check_repository import ROOT, MANIFEST, files, scan


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    release = ROOT / 'workspace/runtime/wtr096-hardening'
    members = {p.relative_to(release / 'skill').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((release / 'skill').rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    canonical = hashlib.sha256(json.dumps(members, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':')).encode()).hexdigest()
    save(release / 'SOURCE-MANIFEST.json', {'version': (release / 'skill/VERSION').read_text('utf-8').strip(),
        'source_file_count': len(members), 'files': members, 'canonical_source_sha256': canonical})
    inventory = {}
    for name in files(ROOT):
        raw = (ROOT / name).read_bytes()
        scan(name, raw)
        inventory[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    save(ROOT / MANIFEST, {'schema': 'walkthrough-source-repository-v1', 'version': '0.8.1-windows-integration-target.16',
                          'files': inventory})
    print(json.dumps({'status': 'manifests_updated', 'files': len(inventory)}))


if __name__ == '__main__':
    main()
