<div align="center">

# ABQ-FLOW

**基于 [python](https://www.python.org/) 的适用于 [Abaqus FEA](https://www.3ds.com/products/simulia/abaqus) 的模块化批处理框架。**

基于策略的批量化运行工作流，支持多类型批量脚本(包括基于修改inp类、基于直接生成cae/inp类)，实现容错、并行执行、资源感知调度等 —— 统一Abaqus CAE的批量仿真工作流

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)  ![Version](https://img.shields.io/badge/version-v0.4.0-green.svg?style=flat-square) ![python](https://img.shields.io/badge/python-3.9+-blue.svg)
<!-- ↑ version badge：发版时需手动同步（单一真相源：pyproject.toml） -->

[English](../README.md) | 简体中文

</div>

## 功能特性

- **⚒️ 基于策略模式的工作流**：将模型准备、结果提取以及仿真过程组合为可复用的流水线。
- **📗 类型化配置**：基于 `JobSpec` 数据类进行配置，在任务执行前完成参数校验，避免运行过程中出现隐蔽的 `KeyError`。
- **🔒 容错并行执行**：采用 `ProcessPoolExecutor` 与 `JobOutcome` 封装执行结果，单个任务失败不会导致整个批处理终止。
- **💻 易于扩展**：无需修改框架源码即可注册并使用自定义的 Preparation Strategy。

## 系统架构

![](docs/image/architecture.png)

## 安装

使用 Pixi：

```bash
pixi add --pypi "ABQflow @ git+https://github.com/Yutu0k/ABQflow.git"
```

- Abaqus（确保命令 `abaqus` 已加入系统 `PATH`）
- Python ≥ 3.9
- [`abqpy`](https://github.com/haiiliin/abqpy)(可选): 安装 `abqpy` 后，可以直接使用普通 `python` 解释器运行脚本，而无需调用 Abaqus 自带的 Python 环境。

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


### 批量参数化任务（Batch Parameterized Job）

当需要针对多组参数进行批量仿真时，可以先定义一个Base Job，然后根据参数数组自动生成多个 `JobSpec`。

```python
import numpy as np
from ABQflow import BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec
from ABQflow import generate_from_array, degenerate_from_array

param_names = ['youngs_modulus', 'load_magnitude']
param_values = np.array([
    [200000, 2000],
    [210000, 3000],
    [220000, 4000],
    [230000, 5000]
])

base_job_spec = JobSpec(
    job_name = "planar_stress_multiple",
    workflow = "modular",
    preparation = PreparationSpec(
        kind = "inp_based",
        source_path = "./examples/cae_file/planar_stress_template.inp",
    ),
    pre_extraction = [
        HookSpec(
            script_path = "./examples/extraction_scripts/get_total_mass.py",
            tasks = [
                {"result_name": "total_mass",},
            ]
        )
    ],
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

spec_list = generate_from_array(
    samples_array = param_values,
    param_names = param_names,
    base_spec  = base_job_spec
)

processor = BatchAbaqusProcessor(
    batch_data = spec_list,
    base_output_dir = os.path.join(CWD, "examples/02_BatchParameterizedJob/output"),
    cpus_per_job = 12,
    duplicate_mode = "overwrite",
    abaqus_exe = ABAQUS_CAE,
)
outcomes = proc.run_batch(num_parallel_jobs=2)

# Get a 2D numpy array of results
arr = degenerate_from_array(outcomes = outcomes, output_names = ["total_mass", "max_stress_mises", "max_displacement"])
print(arr)  # shape (4, 3)
```


### 整体式脚本（Monolithic Script）

TODO

## Hook 脚本

Hook 脚本运行在 **Abaqus Python 解释器**（`abaqus python` 或 `abaqus cae noGUI`）下。ABQflow 提供 **hookkit**——一个单文件、仅依赖标准库的轻量框架，消除全部样板代码，你只需编写物理逻辑。

### 快速入门（ODB 提取）

```python
# my_extract.py
import os, sys
sys.path.insert(0, os.getcwd())     # ABQflow 会自动将 hookkit 复制到工作目录
import hookkit

def extract_one(odb_path, task):
    """输入物理量，输出数值。失败时抛出异常即可。"""
    from odbAccess import openOdb
    name = task['result_name']

    with hookkit.opened(openOdb(path=odb_path, readOnly=True)) as odb:
        step = odb.steps[task.get('step', list(odb.steps.keys())[-1])]
        frame = step.frames[-1]
        asm = odb.rootAssembly

        if name == 'max_stress_mises':
            vals = frame.fieldOutputs['S'].getSubset(
                region=asm.elementSets[' ALL ELEMENTS']).values
            return hookkit.scalar(max(v.mises for v in vals))

        raise ValueError("unsupported result_name: %s" % name)

if __name__ == '__main__':
    hookkit.run(extract_one, source_arg='--odb_path')
```

### 快速入门（INP / mdb 提取）

```python
# my_mass_extract.py
import os, sys
sys.path.insert(0, os.getcwd())
import hookkit

def extract_one(inp_path, task):
    from abaqus import mdb
    name = task['result_name']

    mdb.ModelFromInputFile(name='_hook_temp', inputFileName=inp_path)
    if 'Model-1' in mdb.models:
        del mdb.models['Model-1']

    root_assembly = mdb.models['_hook_temp'].rootAssembly
    region = root_assembly.sets['ALL'].elements

    if name == 'total_mass':
        mass = root_assembly.getMassProperties(regions=region)['mass']
        return hookkit.scalar(mass)

    raise ValueError("unsupported result_name: %s" % name)

if __name__ == '__main__':
    hookkit.run(extract_one, source_arg='--inp_path')
```

### 场量输出（大数据 → CSV 侧载文件）

对于应力场、位移场等大量数据，使用 `hookkit.field()`：

```python
def extract_one(odb_path, task):
    from odbAccess import openOdb
    name = task['result_name']

    with hookkit.opened(openOdb(path=odb_path, readOnly=True)) as odb:
        frame = odb.steps['Step-1'].frames[-1]

        if name == 'stress_field':
            vals = frame.fieldOutputs['S'].values
            rows = [[v.elementLabel, v.mises] for v in vals]
            columns = task.get('columns', ['element_label', 'mises_stress'])
            return hookkit.field(task, rows, columns)

        raise ValueError("unsupported result_name: %s" % name)
```

三种输出模式，由 task dict 中的 `"output"` 控制：

| `"output"` | 行为 |
|------------|------|
| `"inline"` | 始终通过 stdout JSON 返回 |
| `"file"`   | 始终写入 CSV + 返回轻量信封（envelope） |
| （不设置）  | 自动判断：>1 万行 或 >1MB → file，否则 inline |

模式在 Spec 侧声明——hook 脚本无需任何修改：

```python
HookSpec(
    script_path = "./hooks/get_stress_field.py",
    tasks = [{"result_name": "stress_field", "output": "file"}]
)
```

### Task 参数参考

Hook 收到的 `task` 是一个 dict。仅 `result_name` 为必填项；其余 key 均为用户自定义，在 `extract_one` 中通过 `task.get()` 读取。

| Key | 必填 | 使用者 | 用途 |
|-----|------|--------|------|
| `result_name` | **是** | hookkit + 你的代码 | 结果字典的键名；CSV 侧载文件的命名依据 |
| `output` | 否 | `hookkit.field()` | `"inline"` / `"file"`——控制场量表示形态 |
| `step` | 否 | 你的代码 | 指定 ODB 分析步名称（如 `task.get('step', 'Step-1')`） |
| `columns` | 否 | 你的代码 + `hookkit.field()` | CSV 列名（场量输出时使用） |
| *(其他任意)* | 否 | 你的代码 | 自由定义——hookkit 全透明透传 |


---

## Abaqus License Token 并行规划

ABQflow 内置了 Abaqus License Token 计算与并行规划工具，可根据 CPU 数量自动估算所需的 Token 数，并据此确定可同时运行的任务数量。License Token 是硬性限制（超出会导致任务无法启动）；CPU 核数不做硬性限制——小模型往往吃不满一整个核，允许并行数超过物理核数（CPU 超订），只是会打印一条警告提示当前的资源分配情况，具体是否超订由用户自行判断。

```python
from abaqus_batch_pack import solver_tokens, plan_parallelism

# 4 个 CPU 所需的 Token 数：
# ceil(5 * 4^0.422) = 9
print(solver_tokens(4))  # → 9

# 假设可用 45 个 License Token，每个任务用 4 个 CPU（占 9 个 Token）：
print(plan_parallelism(requested=8, cpus_per_job=4, license_tokens=45))  # → 5

# 不传 license_tokens 时不设上限，只在超过物理核数时打印警告：
print(plan_parallelism(requested=8, cpus_per_job=4))  # → 8（并打印 CPU 超订警告）
```

Abaqus 官方推荐的 Token 计算公式为：

```text
T(n) = ⌈5 × n^0.422⌉
```

其中：

- `n` 表示每个任务使用的 CPU 核数；
- `T(n)` 表示对应需要占用的 Abaqus License Token 数量。

框架会综合考虑用户请求的并行任务数、每个任务所需 CPU 数、当前机器可用 CPU 数和Abaqus License Token 数量，自动规划最终能够安全运行的最大并行任务数，避免由于资源不足导致任务失败。

---

## License

本项目采用 **MIT License** 开源协议。

更多信息请参阅：[LICENSE](LICENSE)
