"""Export a conversation copy for the format-6 reader without cache layouts."""

import argparse
import json
from pathlib import Path

from myrk.conversation import ConversationRecord


def main() -> None:
    parser = argparse.ArgumentParser(description="导出供旧版续办的会话副本，保留当前次数与授权，副本不含请求布局。")
    parser.add_argument("source", type=Path, help="当前有效会话记录")
    parser.add_argument("destination", type=Path, help="尚不存在的独立副本路径")
    args = parser.parse_args()
    original = args.source.read_bytes()
    record = ConversationRecord.model_validate_json(original)
    if record.version == 8:
        raise ValueError("格式8保存了会话模型与推理档位，旧版无法无损读取；未创建会丢失选择的副本。")
    if record.version not in (6, 7):
        raise ValueError("只支持格式6或7的有效会话；旧格式须先按原迁移规则升级，不能直接改版本。")
    value = json.loads(original)
    value["version"] = 6
    value.pop("request_layouts", None)
    with args.destination.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(f"已导出格式6会话副本：{args.destination}。保留当前次数与授权；副本不含请求布局。原文件未修改。")


if __name__ == "__main__":
    main()
