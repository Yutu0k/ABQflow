<div align="center">

# ABQ-FLOW

**Modular batch-processing framework for [Abaqus FEA](https://www.3ds.com/products/simulia/abaqus) based on [python](https://www.python.org/).**
Typed job specs, strategy-pattern workflows, fault-tolerant parallel execution, resource-aware scheduling — no more hand-crafted launch scripts.

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)  ![Version](https://img.shields.io/badge/version-v0.4.0-green.svg?style=flat-square) ![python](https://img.shields.io/badge/python-3.9+-blue.svg)
<!-- ↑ version badge: update on release (single source: pyproject.toml) -->

English | [简体中文](../README.zh-CN.md) 

</div>

## Features

- **⚒️ Strategy-pattern workflows**: compose preparation, extraction, and simulation steps into reusable pipelines
- **📗 Typed configuration**: `JobSpec` dataclasses validate before execution, no silent `KeyError` at runtime
- **🔒 Fault-tolerant parallel execution**: `ProcessPoolExecutor` + `JobOutcome` envelope; one failed job won't kill the batch
- **💻 Extensible**: register custom preparation strategies without modifying framework code

## Architecture

<!-- ```
┌──────────────────────────────────────────────────┐
│ User Layer                                        │
│   JobSpec (dataclass, validate + deep-copy)       │
│   BatchSpec = list[JobSpec]                       │
└────────────────┬─────────────────────────────────┘
                 │ StrategyRegistry.build(spec)
┌────────────────▼─────────────────────────────────┐
│ Orchestration Layer  BatchAbaqusProcessor          │
│   plan()      — conflict detection (no side fx)   │
│   prepare()   — apply decisions, build calcs      │
│   run_batch() — ProcessPoolExecutor + fault-tol   │
│   ResourcePlanner — CPU / license constraints     │
└────────────────┬─────────────────────────────────┘
                 │ JobContext (frozen data) + AbaqusRunner
┌────────────────▼─────────────────────────────────┐
│ Execution Layer                                    │
│   JobContext   — paths, name, resources            │
│   AbaqusRunner — run_solver / run_hook             │
│   Strategy     — depends only on (ctx, runner, log)│
└──────────────────────────────────────────────────┘
``` -->

![](https://raw.githubusercontent.com/Yutu0k/ABQflow/main/docs/image/architecture.png)


## Installation

Install from PyPI:

```bash
pip install ABQflow
```

If you want the optional `abqpy` integration path:

```bash
pip install "ABQflow[abqpy]"
```

If you manage environments with Pixi:

```bash
pixi add --pypi ABQflow
```

**Prerequisites:** Abaqus (with `abaqus` on PATH), Python ≥ 3.9. `abqpy` is optional and only needed when you want to run eligible scripts through plain `python` instead of the Abaqus kernel.

## Packaging

Build a source distribution and wheel locally with:

```bash
python -m build
```

Then upload the artifacts from `dist/` to TestPyPI or PyPI.

## How to use?

### Single parameterized job

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

### Batch parameterized job

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

### Monolithic script

<!-- ```python
jobs = [{
    'job_name': 'full_model',
    'workflow': 'monolithic',
    'script_path': './build_and_run.py',
    'params': {'length': 100, 'mesh_size': 2.0},
}]

proc = BatchAbaqusProcessor(jobs, './output', cpus_per_job=4)
outcomes = proc.run_batch(num_parallel_jobs=1)
``` -->

TODO


## Hook Scripts

Extraction hooks run under the **Abaqus Python interpreter** (`abaqus python` or `abaqus cae noGUI`). ABQflow provides **hookkit** — a single-file, stdlib-only harness that eliminates all boilerplate. You write only the physics.

### Quick start (ODB)

```python
# my_extract.py
import os, sys
sys.path.insert(0, os.getcwd())     # hookkit is staged here by ABQflow
import hookkit

def extract_one(odb_path, task):
    """Physics in, value out. Raise on failure."""
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

### Quick start (INP / mdb)

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

### Field output (large data → CSV sidecar)

For field quantities (stress tensors, displacement fields), use `hookkit.field()`:

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

Three output modes, controlled by `"output"` in the task dict:

| `"output"` | Behavior |
|------------|----------|
| `"inline"` | Return through stdout JSON (always) |
| `"file"`   | Write CSV + return a lightweight envelope (always) |
| (unset)    | Auto: >10k rows or >1MB → file, else inline |

Mode is declared in the Spec — the hook script stays the same:

```python
HookSpec(
    script_path = "./hooks/get_stress_field.py",
    tasks = [{"result_name": "stress_field", "output": "file"}]
)
```

### Task dict reference

Your hook receives tasks as `task` dicts. Only `result_name` is required; every other key is user-defined and read by your `extract_one` via `task.get()`.

| Key | Required | Used by | Purpose |
|-----|----------|---------|---------|
| `result_name` | **yes** | hookkit + your code | Result key in the output dict; file name for sidecar CSV |
| `output` | no | `hookkit.field()` | `"inline"` / `"file"` — controls field representation |
| `step` | no | your code | ODB step name (e.g. `task.get('step', 'Step-1')`) |
| `columns` | no | your code + `hookkit.field()` | CSV column headers for field output |
| *(any other)* | no | your code | Freely defined — hookkit passes everything through transparently |


## License Token Planning

```python
from abaqus_batch_pack import solver_tokens, plan_parallelism

# Tokens for 4 CPUs: ceil(5 * 4^0.422) = 9
print(solver_tokens(4))  # → 9

# Max parallel jobs on a 16-core machine with 4 CPUs/job
print(plan_parallelism(requested=8, cpus_per_job=4))  # → 3
```

Formula: `T(n) = ⌈5 × n^0.422⌉` (Abaqus official)


## License

MIT License | See [LICENSE](LICENSE) for more details
