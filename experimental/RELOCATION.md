# 实验目录迁移

2026-11-16，在 `kun` 分支将原根目录的实现、测试、项目配置、技术笔记及历史参考代码迁入本目录。
根目录保留入口README、Git配置和测试路由，没有提交或推送至main。

- 包名仍是 `fairy`、`evaluate`、`interface`，没有改成 `experimental.fairy`。
- 项目根改为 `experimental/`；仓库根安装使用 `pip install -e './experimental[...]'`。
- 原`.venv`保持原位并重新执行editable安装，避免直接搬迁虚拟环境破坏脚本绝对路径。
- 原`.runs`及历史JSON、轮包、校验和保持原位；源码身份按包相对路径计算，不依赖仓库位置。
- `reference/`整体移入此目录，仍被根`.gitignore`忽略，没有编辑其中历史源码。
- 根`pytest.ini`指向`experimental/demo`，根目录与本目录均有测试入口。
- 维护中的相邻HSCC及根`.runs`文档链接已调整，历史报告的旧命令和绝对路径保留当时含义。

详细运行见[实验实现](README.md)，场景设计与验收见[构建合同](evaluate/scenarios/CONSTRUCTION.md)。
