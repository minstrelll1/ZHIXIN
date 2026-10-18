#!/usr/bin/env python3
"""Verify inputs, hash all deliverables, create and verify a portable ZIP."""
import hashlib, json, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
EXCLUDE_FILES={'SHA256SUMS.json','SHA256SUMS.txt'}
EXCLUDE_PARTS={'__pycache__','node_modules','.pytest_cache','.venv'}

def sha(data):return hashlib.sha256(data).hexdigest()
def included(path):
    rel=path.relative_to(ROOT)
    return path.is_file() and not any(x in EXCLUDE_PARTS for x in rel.parts) and path.suffix not in {'.pyc','.pyo'} and rel.as_posix() not in EXCLUDE_FILES

def main():
    expected=json.loads((ROOT/'inputs/07_文件校验.json').read_text(encoding='utf-8'))['files']
    for name,want in expected.items():
        got=sha((ROOT/'inputs'/name).read_bytes())
        if got!=want:raise RuntimeError('Original input hash mismatch: '+name)
    files=sorted((p for p in ROOT.rglob('*') if included(p)),key=lambda p:p.relative_to(ROOT).as_posix())
    hashes={p.relative_to(ROOT).as_posix():sha(p.read_bytes()) for p in files}
    manifest=dict(algorithm='SHA256',root='planning_results/',original_input_hashes_verified=True,hash_scope='All included regular files except these two self-referential SHA256SUMS manifests. ZIP and ZIP sidecar are outside the package. Exclude Python caches, node_modules and virtual environments.',files=hashes)
    (ROOT/'SHA256SUMS.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'SHA256SUMS.txt').write_text(''.join(v+'  '+k+'\n' for k,v in hashes.items()),encoding='utf-8')
    archive=ROOT.parent/'planning_results.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in files+[ROOT/'SHA256SUMS.json',ROOT/'SHA256SUMS.txt']:
            # Fixed archive metadata gives deterministic ZIP for identical bytes.
            info=zipfile.ZipInfo('planning_results/'+p.relative_to(ROOT).as_posix(),date_time=(2026,10,17,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            z.writestr(info,p.read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    with zipfile.ZipFile(archive) as z:
        bad=z.testzip()
        if bad:raise RuntimeError('ZIP CRC failed: '+bad)
        for name,want in hashes.items():
            if sha(z.read('planning_results/'+name))!=want:raise RuntimeError('ZIP hash mismatch: '+name)
        if len(z.namelist())!=len(hashes)+2:raise RuntimeError('Unexpected archive entries')
    digest=sha(archive.read_bytes());sidecar=archive.with_suffix(archive.suffix+'.sha256');sidecar.write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
    print(json.dumps(dict(archive=str(archive),bytes=archive.stat().st_size,entries=len(hashes)+2,sha256=digest,verified=True),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
