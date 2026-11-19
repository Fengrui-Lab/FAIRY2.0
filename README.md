# FAIRY2.0 · 实验工作区

当前 `kun` 分支用于验证 FAIRY2.0 的重建方案，尚不是公开稳定发行版。
实现、测试和详细技术说明集中在 [`experimental/`](experimental/README.md)。

```text
FAIRY2.0/
├── README.md
├── experimental/
│   ├── fairy/
│   ├── evaluate/
│   ├── interface/
│   ├── demo/
│   ├── reference/          # 可选的本地历史源码，不入库
│   └── pyproject.toml
├── .venv/                  # 本机环境，不入库
└── .runs/                  # 历史和本机验收证据，不入库
```

在仓库根目录创建虚拟环境、安装和执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e './experimental[wofost,llm,oracle,test,typing]'
.venv/bin/python -m pytest -q --disable-warnings
.venv/bin/python -m interface.cli run --backend native
```

只验证 Native 可安装 `./experimental[test]`。运行和测试不需要本地历史源码或真实 LLM 凭证；历史版本索引见 [`reference-baseline.json`](experimental/reference-baseline.json)。从 `experimental/` 内运行时，用 `../.venv/bin/python`；
相对输出路径以实际工作目录为准。已有产物不覆盖，新运行请使用新路径或 CLI 自动生成的路径。

迁移没有改写历史实验产物，也没有移动现有虚拟环境。历史记录中的旧绝对路径仅表示当时运行位置。
详细修复与兼容性边界见 [HSCC 修复报告](experimental/demo/HSCC_FIXES.md)。

当前首轮场景支持HEINONG58 normal、R1干旱、R5干旱及R1＋R5组合。
设计与对照验证命令见[场景构建合同](experimental/evaluate/scenarios/CONSTRUCTION.md)，
目录调整见[迁移说明](experimental/RELOCATION.md)。

本轮完成情况与验证结果见[0.6.0 场景及迁移验收](experimental/demo/SCENARIO_CONSTRUCTION.md)。

自动Oracle首轮开发、真实训练与候选验证方法见[实施说明](experimental/demo/AUTO_ORACLE_IMPLEMENTATION.md)。

WOFOST全季及自动Oracle接入（0.8.1，代理品种，非黑农58校准）见
[实施与验收](experimental/demo/WOFOST_FULL_SEASON.md)和
[后端过程说明](experimental/fairy/acps/backends/biology/wofost/README.md)。

逐模块阅读、检查与修改可从[代码框架审查与修改指导书](experimental/CODE_REVIEW_GUIDE.md)开始，含实际能力地图、M00–M14检查表、已知缺口及验证入口。

0.8.2全模块类型、核心注释与IDE导航见[类型与导航说明](experimental/TYPING.md)。
