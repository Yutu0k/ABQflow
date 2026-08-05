"""Shared path constants for the real-Abaqus integration suite.

Resolved from this file's location (not the pytest invocation cwd) so the
suite works whether pytest is run from the repo root or anywhere else.

Not named ``conftest.py`` on purpose — pytest would otherwise import it
under the same bare module name as ``test/conftest.py`` (default "prepend"
import mode has no package markers here), which collides.
"""

import os

_INTEGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_INTEGRATION_DIR))
EXAMPLES_DIR = os.path.join(REPO_ROOT, 'examples')
CAE_FILE_DIR = os.path.join(EXAMPLES_DIR, 'cae_file')
EXTRACTION_SCRIPTS_DIR = os.path.join(EXAMPLES_DIR, 'extraction_scripts')

# Self-contained template (has {{youngs_modulus}}/{{load_magnitude}} placeholders,
# resolved by kind='inp_based') — mirrors examples/01_SingleParameterizedJob.
TEMPLATE_INP = os.path.join(CAE_FILE_DIR, 'planar_stress_template.inp')

# Pre-existing, ready-to-run INP that *INCLUDEs ../planar_stress_main.inp for
# geometry — mirrors examples/04_PreflightAndDiagnostics (kind='existing_inp').
SCENARIO_1_INP = os.path.join(CAE_FILE_DIR, 'scenarios', 'planar_stress_scenario_1.inp')

GET_MAX_STRESS_SCRIPT = os.path.join(EXTRACTION_SCRIPTS_DIR, 'get_max_stress_mises.py')
GET_TOTAL_MASS_SCRIPT = os.path.join(EXTRACTION_SCRIPTS_DIR, 'get_total_mass.py')
