<div align="center">

# ABQ-FLOW

**Modular batch-processing framework for [Abaqus FEA](https://www.3ds.com/products/simulia/abaqus).**
Typed job specs, strategy-pattern workflows, fault-tolerant parallel execution, resource-aware scheduling — no more hand-crafted launch scripts.

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)  ![Version](https://img.shields.io/badge/version-v0.3.0-green.svg?style=flat-square) ![python](https://img.shields.io/badge/python-3.9+-blue.svg)

English | [简体中文](doc/README.zh-CN.md) 

</div>

## Features

- **Strategy-pattern workflows** — compose preparation, extraction, and simulation steps into reusable pipelines
- **Typed configuration** — `JobSpec` dataclasses validate before execution, no silent `KeyError` at runtime
- **Fault-tolerant parallel execution** — `ProcessPoolExecutor` + `JobOutcome` envelope; one failed job won't kill the batch
- **Resource-aware scheduling** — CPU-core and Abaqus-license-token constraints with automatic parallelism reduction
- **Sentinel-based JSON protocol** — reliable stdout parsing even with Abaqus banner noise
- **Extensible** — register custom preparation strategies without modifying framework code
- **CI-friendly** — zero-interaction defaults; `duplicate_mode='fail'` by default

## Architecture

```
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
```


## Installation

```bash
pixi add --pypi "abq_flow @ git+https://github.com/Yutu0k/abaqus-batch-pack.git"
```

**Prerequisites:** Abaqus (with `abaqus` on PATH), Python ≥ 3.9. Optional: [`abqpy`](https://github.com/haiiliin/abqpy) for running scripts directly with `python` instead of the Abaqus kernel.

## How to use?

### Single parameterized job

```python
from abaqus_batch_pack import BatchAbaqusProcessor

jobs = [{
    'job_name': 'cantilever_01',
    'type': 'inp_based',
    'base_inp_path': './cantilever.inp',
    'params': {'E': 210000, 'F': 1000},
    'post_extraction': [{
        'script_path': './get_deflection.py',
        'tasks': [{'result_name': 'max_u'}]
    }]
}]

proc = BatchAbaqusProcessor(jobs, './output', cpus_per_job=4)
outcomes = proc.run_batch(num_parallel_jobs=1)

for oc in outcomes:
    print(f"{oc.job_name}: {oc.status} → {oc.results}")
```

### Parameter sweep

```python
import numpy as np
from abaqus_batch_pack import generate_from_array, BatchAbaqusProcessor, degenerate_from_array

base = {
    'job_name': 'sweep',
    'type': 'inp_based',
    'base_inp_path': './template.inp',
    'post_extraction': [{
        'script_path': './extract.py',
        'tasks': [{'result_name': 'stress'}, {'result_name': 'mass'}]
    }]
}

samples = np.array([[200e3, 0.3], [210e3, 0.3], [200e3, 0.33]])
specs = generate_from_array(samples, ['E', 'nu'], base)

proc = BatchAbaqusProcessor(specs, './output', cpus_per_job=4)
outcomes = proc.run_batch(num_parallel_jobs=2)

# Get a 2D numpy array of results
arr = degenerate_from_array(outcomes, ['stress', 'mass'])
print(arr)  # shape (3, 2)
```

### Monolithic script

```python
jobs = [{
    'job_name': 'full_model',
    'workflow': 'monolithic',
    'script_path': './build_and_run.py',
    'params': {'length': 100, 'mesh_size': 2.0},
}]

proc = BatchAbaqusProcessor(jobs, './output', cpus_per_job=4)
outcomes = proc.run_batch(num_parallel_jobs=1)
```

Your script writes results via sentinel markers:

```python
import sys, json
# ... build model, run job, extract results ...
results = {'status': 'COMPLETED', 'max_stress': 4525.3}
sys.__stdout__.write(f"===ABQ_RESULT_BEGIN===\n{json.dumps(results)}\n===ABQ_RESULT_END===\n")
```

See the [full architecture docs]() for design rationale.

## Hook Scripts

Extraction hook should follow the precedure below:



> Quick Example:
> ```python
> # my_extract.py
> import argparse, sys, json
> from odbAccess import openOdb
> 
> parser = argparse.ArgumentParser()
> parser.add_argument('--odb_path', required=True)
> parser.add_argument('--tasks_json', required=True)
> args = parser.parse_args()
> 
> with open(args.tasks_json) as f:
>     tasks = json.load(f)
> 
> odb = openOdb(args.odb_path)
> results = {}
> for task in tasks:
>     name = task['result_name']
>     try:
>         results[name] = 123.45  # your extraction logic
>     except Exception:
>         results[name] = None
> 
> odb.close()
> sys.__stdout__.write(json.dumps(results))
> ```

Use `--inp_path` instead of `--odb_path` for pre-simulation extraction (model properties).


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
