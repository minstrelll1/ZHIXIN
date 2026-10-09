#!/usr/bin/env python3
"""构建后只读核对 ROS 消息，避免旧编译产物导致回传订阅失败。"""
import argparse
import importlib
from pathlib import Path
import sys

MESSAGE_NAMES = ("CompletedTarget", "CompletedTargetArray", "TargetDetection")


def source_contracts(root):
    # 使用 ROS 官方 genmsg 从当前源码算 MD5，不使用可能陈旧的生成类。
    import genmsg
    import genmsg.gentools
    import genmsg.msg_loader
    import rospkg

    message_dir = root / "src" / "su17_image_transfer" / "msg"
    context = genmsg.MsgContext.create_default()
    search = {
        "su17_image_transfer": [str(message_dir)],
        "std_msgs": [str(Path(rospkg.RosPack().get_path("std_msgs")) / "msg")],
    }
    result = {}
    for name in MESSAGE_NAMES:
        spec = genmsg.msg_loader.load_msg_from_file(
            context, str(message_dir / (name + ".msg")), "su17_image_transfer/" + name)
        genmsg.msg_loader.load_depends(context, spec, search)
        result[name] = (genmsg.gentools.compute_md5(context, spec),
                        tuple(spec.names), tuple(spec.types))
    return result


def verify_generated(root, model, contracts):
    expected_dir = (root / ("devel_" + model)).resolve()
    errors, verified = [], []
    for name, (md5sum, names, types) in contracts.items():
        module = importlib.import_module("su17_image_transfer.msg._" + name)
        location = Path(module.__file__).resolve()
        try:
            location.relative_to(expected_dir)
        except ValueError:
            errors.append("%s 从其他工作空间加载：%s" % (name, location))
            continue
        message = getattr(module, name)
        actual_md5 = getattr(message, "_md5sum", "")
        if (actual_md5 != md5sum or
                tuple(getattr(message, "__slots__", ())) != names or
                tuple(getattr(message, "_slot_types", ())) != types):
            errors.append("%s 的源码与生成消息不一致：源码 MD5=%s，生成 MD5=%s，文件=%s"
                          % (name, md5sum, actual_md5, location))
        else:
            verified.append((name, md5sum, location))
    if errors:
        raise RuntimeError("\n".join(errors))
    return verified


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("p600", "su17"), required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        verified = verify_generated(root, args.model, source_contracts(root))
    except Exception as exc:
        print("机载消息校验失败：%s\n本次部署尚未完成，请保留编译输出并重新构建。"
              % exc, file=sys.stderr)
        return 1
    for name, md5sum, location in verified:
        print("机载消息校验通过：%s，MD5=%s，文件=%s" % (name, md5sum, location))
    print("竞赛消息已与当前源码一致；已运行的机载竞赛程序需停止后重新启动才会使用新消息。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
