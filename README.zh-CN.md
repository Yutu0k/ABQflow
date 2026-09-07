<div align="center">

# ABQ-FLOW

**基于 [python](https://www.python.org/) 的适用于 [Abaqus FEA](https://www.3ds.com/products/simulia/abaqus) 的模块化批处理框架。**

基于策略的批量化运行工作流，支持多类型批量脚本(包括基于修改inp类、基于直接生成cae/inp类)，实现容错、并行执行、资源感知调度等 —— 统一Abaqus CAE的批量仿真工作流

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)  ![Version](https://img.shields.io/badge/version-v0.6.0-green.svg?style=flat-square) ![python](https://img.shields.io/badge/python-3.9+-blue.svg)
<!-- ↑ version badge：发版时需手动同步（单一真相源：pyproject.toml） -->

[English](../README.md) | 简体中文

</div>

## 功能特性

- **⚒️ 基于策略模式的工作流**：将模型准备、结果提取以及仿真过程组合为可复用的流水线。
- **📗 类型化配置**：基于 `JobSpec` 数据类进行配置，在任务执行前完成参数校验，避免运行过程中出现隐蔽的 `KeyError`。
- **🔒 容错并行执行**：采用 `ProcessPoolExecutor` 与 `JobOutcome` 封装执行结果，单个任务失败不会导致整个批处理终止。
- **💻 易于扩展**：无需修改框架源码即可注册并使用自定义的 Preparation Strategy。

## 安装

从 PyPI 安装：

```bash
pip install ABQflow
```

如果需要可选的 `abqpy` 集成路径：

```bash
pip install "ABQflow[abqpy]"
```

如果你使用 Pixi 管理环境：

```bash
pixi add --pypi ABQflow
```

- Abaqus（确保命令 `abaqus` 已加入系统 `PATH`）
- Python ≥ 3.9
- [`abqpy`](https://github.com/haiiliin/abqpy)（可选）：仅在你想让部分脚本直接通过普通 `python` 运行时需要。

## 打包发布

先在本地构建分发包：

```bash
python -m build
```

然后把 `dist/` 目录中的产物上传到 TestPyPI 或正式 PyPI。

## 如何使用？

### 单个参数化任务（Single Parameterized Job）

下面的示例展示了如何定义并运行一个参数化 Abaqus 仿真任务


```python
from ABQflow import BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec

spec = JobSpec(
    job_name = "planar_stress_odb",
    workflow = "modular",
    preparation = PreparationSpec(
        kind = "inp_based",
        source_path = "./examples/cae_file/planar_stress_template.inp",
        params = {
            "youngs_modulus": 210000,
            "load_magnitude": 2000,
        }
    ),
    post_extraction = [
        HookSpec(
            script_path = "./examples/extraction_scripts/get_max_stress_mises.py",
            tasks = [
                {"result_name": "max_stress_mises",},
                {"result_name": "max_displacement",},
            ]
        )
    ]
)

processor_odb = BatchAbaqusProcessor(
    batch_data = [spec],
    base_output_dir = os.path.join(CWD, "examples/01_SingleParameterizedJob/output"),
    cpus_per_job = 4,
    duplicate_mode = "overwrite",
    abaqus_exe = ABAQUS_CAE,
)
outcomes = processor.run_batch(num_parallel_jobs=1)

for oc in outcomes:
    print(f"{oc.job_name}: {oc.status} → {oc.results}")
```

## License

本项目采用 **MIT License** 开源协议。

更多信息请参阅：[LICENSE](LICENSE)
