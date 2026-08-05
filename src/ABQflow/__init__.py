"""
Key modules
-----------
- AbaqusCalculation
- BatchAbaqusProcessor
- JobSpec
- PreparationSpec
- HookSpec

Key Methods
-----------
- degenerate_from_array
- generate_from_array
- generate_from_inp_files
- outcomes_to_dict
- outcomes_to_list

"""


import re
from pathlib import Path

from .core.abaqus_automation import (
	AbaqusCalculation,
	BatchAbaqusProcessor,
	JobOutcome,
	JobPlan,
	plan_parallelism,
	solver_tokens,
)
from .core.context import JobContext
from .core.diagnostics import (
	SolverDiagnostics,
	SolverResult,
	apply_truth_table,
	diagnose,
	harvest_errors,
	parse_sta,
)
from .core.registry import PREPARATION_REGISTRY, build_workflow, register_preparation
from .core.runner import AbaqusRunner, CommandRecord, extract_json
from .core.spec import HookSpec, JobSpec, PreparationSpec, SubroutineSpec
from .core.status import JobStatus, JobStatusManager
from .core.strategies import (
	ExistingInpStrategy,
	ExtractionStrategy,
	InpModifyStrategy,
	JobWorkflowStrategy,
	ModelGenerationStrategy,
	ModelPropertiesExtractionStrategy,
	ModularWorkflowStrategy,
	MonolithicWorkflowStrategy,
	OdbExtractionStrategy,
	PreparationStrategy,
	SubroutineCompileStrategy,
)
from .helpers.constant import (
	RESULT_BEGIN,
	RESULT_END,
)
from .helpers.convert import (
	degenerate_from_array,
	generate_from_array,
	generate_from_inp_files,
	is_sidecar,
	iter_fields,
	load_field,
	outcomes_to_dict,
	outcomes_to_list,
	resolve_sidecar,
	sanitize_job_name,
)

__all__ = [
	"PREPARATION_REGISTRY",
	"RESULT_BEGIN",
	"RESULT_END",
	# Core — orchestration
	"AbaqusCalculation",
	"AbaqusRunner",
	"BatchAbaqusProcessor",
	"CommandRecord",
	"ExistingInpStrategy",
	"ExtractionStrategy",
	"HookSpec",
	"InpModifyStrategy",
	# Core — context & runner
	"JobContext",
	"JobOutcome",
	"JobPlan",
	# Core — spec
	"JobSpec",
	# Core — status
	"JobStatus",
	"JobStatusManager",
	"JobWorkflowStrategy",
	"ModelGenerationStrategy",
	"ModelPropertiesExtractionStrategy",
	"ModularWorkflowStrategy",
	"MonolithicWorkflowStrategy",
	"OdbExtractionStrategy",
	"PreparationSpec",
	# Core — strategies
	"PreparationStrategy",
	# Core — diagnostics
	"SolverDiagnostics",
	"SolverResult",
	"SubroutineCompileStrategy",
	"SubroutineSpec",
	"apply_truth_table",
	# Core — registry
	"build_workflow",
	"degenerate_from_array",
	"diagnose",
	"extract_json",
	# Helpers
	"generate_from_array",
	"generate_from_inp_files",
	"harvest_errors",
	"is_sidecar",
	"iter_fields",
	"load_field",
	"outcomes_to_dict",
	"outcomes_to_list",
	"parse_sta",
	# Core — resource planning
	"plan_parallelism",
	"register_preparation",
	"resolve_sidecar",
	"sanitize_job_name",
	"solver_tokens",
]

def _get_version() -> str:
	"""Read the package version from pyproject.toml (single source of truth)."""
	# 1. Prefer importlib.metadata (works when package is installed)
	try:
		from importlib.metadata import version as _meta_version
		return _meta_version("ABQflow")
	except Exception:
		pass
	# 2. Fallback: parse pyproject.toml directly (works in dev / editable installs)
	try:
		_pyproject = Path(__file__).parents[2] / "pyproject.toml"
		if _pyproject.exists():
			match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', _pyproject.read_text(encoding="utf-8"))
			if match:
				return match.group(1)
	except Exception:
		pass
	# 3. Last resort
	return "0.0.0"

__version__ = _get_version()
