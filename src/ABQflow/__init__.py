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


from pathlib import Path
import re

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
from .helpers.constant import (
	RESULT_BEGIN,
	RESULT_END,
)

__all__ = [
	# Core — orchestration
	"AbaqusCalculation",
	"BatchAbaqusProcessor",
	"JobOutcome",
	"JobPlan",
	# Core — context & runner
	"JobContext",
	"AbaqusRunner",
	"CommandRecord",
	"extract_json",
	# Core — spec
	"JobSpec",
	"HookSpec",
	"PreparationSpec",
	"SubroutineSpec",
	# Core — registry
	"build_workflow",
	"register_preparation",
	"PREPARATION_REGISTRY",
	# Core — status
	"JobStatus",
	"JobStatusManager",
	# Core — strategies
	"PreparationStrategy",
	"ExistingInpStrategy",
	"InpModifyStrategy",
	"ModelGenerationStrategy",
	"ExtractionStrategy",
	"OdbExtractionStrategy",
	"ModelPropertiesExtractionStrategy",
	"JobWorkflowStrategy",
	"MonolithicWorkflowStrategy",
	"ModularWorkflowStrategy",
	"SubroutineCompileStrategy",
	# Core — diagnostics
	"SolverDiagnostics",
	"SolverResult",
	"diagnose",
	"harvest_errors",
	"parse_sta",
	"apply_truth_table",
	# Core — resource planning
	"plan_parallelism",
	"solver_tokens",
	# Helpers
	"generate_from_array",
	"generate_from_inp_files",
	"sanitize_job_name",
	"degenerate_from_array",
	"outcomes_to_list",
	"outcomes_to_dict",
	"is_sidecar",
	"resolve_sidecar",
	"load_field",
	"iter_fields",
	"RESULT_BEGIN",
	"RESULT_END",
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
