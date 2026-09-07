<div align="center">

# ABQ-FLOW

**Modular batch-processing framework for [Abaqus FEA](https://www.3ds.com/products/simulia/abaqus) based on [python](https://www.python.org/).**
Typed job specs, strategy-pattern workflows, fault-tolerant parallel execution, resource-aware scheduling — no more hand-crafted launch scripts.

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)  ![Version](https://img.shields.io/badge/version-v0.5.1-green.svg?style=flat-square) ![python](https://img.shields.io/badge/python-3.9+-blue.svg)
<!-- ↑ version badge: update on release (single source: pyproject.toml) -->

English | [简体中文](../README.zh-CN.md) 

</div>

## Features

- **⚒️ Strategy-pattern workflows**: compose preparation, extraction, and simulation steps into reusable pipelines
- **📗 Typed configuration**: `JobSpec` dataclasses validate before execution, no silent `KeyError` at runtime
- **🔒 Fault-tolerant parallel execution**: `ProcessPoolExecutor` + `JobOutcome` envelope; one failed job won't kill the batch
- **💻 Extensible**: register custom preparation strategies without modifying framework code

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


## License

MIT License | See [LICENSE](LICENSE) for more details
