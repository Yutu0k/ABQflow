# Examples

- [x] `01_SingleParameterizedJob`
- [x] `02_BatchParameterizedJob`
- [x] `03_ExistinglnpBatchJob`
- [x] `04_PreflightAndDiagnostics`
- [x] `05_DryRunPreview`
- [x] `06_SeparateJob`
- [x] `07_SubroutineJob`
- [x] `08_RemoteSubmission`

## `extraction_scripts/`

Hook scripts for the post-processing phase.

| Script | Source | Notes |
|--------|--------|-------|
| `get_max_stress_mises.py` | ODB | The canonical hook — the shape to copy |
| `get_total_mass.py` | INP | Pre-extraction, runs in the CAE kernel |
| `reference_field_hook.py` | ODB | `hookkit.field()` reference: inline vs CSV sidecar |
| `get_dat_results.py` | `.dat` | Same shape as the ODB hook, with `datkit` instead of `odbAccess` |

`get_dat_results.py` reads values written by `*NODE PRINT` / `*EL PRINT`, for
when the ODB is too large to open. Point a `HookSpec(..., source="dat")` at it
and ABQflow runs it under the host Python — no solver, no license token. The
parsing is `ABQflow.datkit`, which is staged into the job directory alongside
`hookkit.py`; see the `.dat` section of the
[README](../README.md#reading-the-dat-instead-of-the-odb).