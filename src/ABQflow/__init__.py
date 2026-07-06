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
- outcomes_to_dict
- outcomes_to_list

"""


from .core.abaqus_automation import (
	AbaqusCalculation,
	BatchAbaqusProcessor,
	JobOutcome,
	plan_parallelism,
	solver_tokens,
)
from .core.context import JobContext
from .core.registry import PREPARATION_REGISTRY, build_workflow, register_preparation
from .core.runner import AbaqusRunner, extract_json
from .core.spec import HookSpec, JobSpec, PreparationSpec
from .core.status import JobStatus, JobStatusManager
from .core.strategies import (
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
	outcomes_to_dict,
	outcomes_to_list,
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
	# Core — context & runner
	"JobContext",
	"AbaqusRunner",
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
	"InpModifyStrategy",
	"ModelGenerationStrategy",
	"ExtractionStrategy",
	"OdbExtractionStrategy",
	"ModelPropertiesExtractionStrategy",
	"JobWorkflowStrategy",
	"MonolithicWorkflowStrategy",
	"ModularWorkflowStrategy",
	# Helpers
	"generate_from_array",
	"degenerate_from_array",
	"outcomes_to_list",
	"outcomes_to_dict",
	"RESULT_BEGIN",
	"RESULT_END",
	# Core — resource planning
	"plan_parallelism",
	"solver_tokens",
]

__version__ = "0.3.0"
