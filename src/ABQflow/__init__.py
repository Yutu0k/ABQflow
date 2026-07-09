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
from .core.spec import HookSpec, JobSpec, PreparationSpec
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
)

from .helpers.convert import (
	degenerate_from_array,
	generate_from_array,
	generate_from_inp_files,
	is_sidecar,
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
	"RESULT_BEGIN",
	"RESULT_END",
]

__version__ = "0.3.0"
