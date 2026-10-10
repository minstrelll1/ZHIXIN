"""从指定 Git 提交生成增量发布清单；不读取工作区、本机令牌或飞行数据。"""
import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", *args])


def build_manifest(repository, branch, revision="HEAD"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("仓库名称无效")
    if not branch or any(part in ("", ".", "..") for part in branch.split("/")) or "\\" in branch:
        raise ValueError("分支名称无效")
    commit = git("rev-parse", "--verify", revision + "^{commit}").decode().strip()
    tree_sha = git("rev-parse", commit + "^{tree}").decode().strip()
    files = []
    seen = set()
    for entry in git("ls-tree", "--full-tree", "-r", "-l", "-z", commit).split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, sha, size = meta.decode("ascii").split()
        path = path.decode("utf-8")
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("发布目录包含不支持的文件或子模块：" + path)
        if (path.casefold() in seen or "\\" in path or re.search(r'[:*?"<>|]', path)
                or any(not part or part in (".", "..") or part.endswith((".", " "))
                       or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", part, re.I)
                       for part in path.split("/"))):
            raise ValueError("发布路径不兼容 Windows：" + path)
        seen.add(path.casefold())
        files.append(dict(path=path, type=kind, mode=mode, sha=sha, size=int(size)))
    if not files:
        raise ValueError("提交不包含可发布文件")
    return dict(schema_version=1, repository=repository, branch=branch, commit=commit,
                tree_sha=tree_sha, file_count=len(files), files=files,
                generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(args.repository, args.branch, args.commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("增量清单生成完成：%s，共%d个文件" % (manifest["commit"][:7], manifest["file_count"]))


if __name__ == "__main__":
    main()
