# 0.5.0

## Added

- [x] add JobOutcome processor in `convert.py`
- [x] allow modular workflow (See `./examples/06_SeparateJob`)
- [x] add subroutine support (See `./examples/07_SubroutineJob`)

## Fixed

- [x] Now use physical core when running ABAQUS
- [x] Remove cpus oversubsciption limit. Only gives warning when exceeds cpu limit
- [x] Fix `BatchAbaqusProcessor` log issue, now logs more after running
- [x] Fix Unused Status
- [x] Remove some typos

# 0.4.0

## Added

- [x] Rename project from `abq-flow` to `ABQflow`
- [x] Allow direct INP-based batch runs on existing INP files (See `./examples/03_ExistingInpBatchJob`)
- [x] Add full documentation site (Sphinx + README/README.zh-CN) and expanded examples

## Fixed

- [x] Reframe package structure into `core`/`helpers` submodules
- [x] Fix error extraction during job execution
- [x] Fix example file structure, add dedicated extraction script examples

# 0.3.0

## Added

- [x] Split the `AbaqusCalculation` god-object into `JobContext` (frozen data) + `AbaqusRunner` (service) + Strategy pattern
- [x] Typed `JobSpec` / `PreparationSpec` / `HookSpec` configuration replacing dict-based config (`from_dict` bridge kept for backward compatibility)
- [x] `run_batch()` now returns `list[JobOutcome]` instead of `list[dict]` / `dict[str, dict]`

## Fixed

- [x] Fix structural defects in `AbaqusCalculation` (circular imports, private-method coupling) by splitting data from service
- [x] Custom strategy signatures changed from `(self, context: AbaqusCalculation)` to `(self, ctx: JobContext, runner: AbaqusRunner, logger: Logger)`
- [x] `BatchAbaqusProcessor.__init__` no longer deletes directories or prompts for input; default `duplicate_mode` changed from `'interactive'` to `'fail'`

# 0.2.0

## Added

- [x] Allow multiple extractions within a single script
- [x] Add `tqdm` progress bars
- [x] Support async execution, allow in-time progress bar updates
- [x] Add `StatusManager`, fix batch job naming
- [x] Allow `skip` / `overwrite` / `rename` duplicate-handling modes when creating `AbaqusCalculation` instances

## Fixed

- [x] Fix `StatusManager`
- [x] Improve docstrings

# 0.1.0

## Added

- [x] Initial release: core batch Abaqus automation prototype
