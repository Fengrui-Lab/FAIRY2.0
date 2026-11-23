# 离线实验

`historical/cli.py` 是历史天气实验入口，各场景的构建与验收放在
`historical/cases/`。公开操作辅助、切片、天气获取和 WOFOST 干旱批量验证
由 `historical/` 下的模块提供。

`validation/` 放合成干旱、Native 正常与组合场景、WOFOST 代理场景的离线验证。
历史实验和这三项验证都提供原模块路径的命令兼容入口。

实验加载 [场景库](../scenarios/README.md) 的声明并使用统一 Runtime。
场景编译和恢复校验可独立加载。命令与产物示例见
[历史试点说明](../scenarios/HISTORICAL_PILOT.md)。
