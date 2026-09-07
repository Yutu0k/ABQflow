Prepare INP Template
====================

To run a batch of jobs, we need to prepare a batch of INP files. The simplest way is to prepare a single INP template file with ``{{placeholders}}`` that get replaced per job. 

.. note::

    In the future, ABQflow plans to support extracting batch parameters from multiple existing INP files.


This example gives a simple example of preparing an INP template file. The template file is located in ``./examples/cae_file/planar_stress_template.inp``. Here we add two placeholders ``{{youngs_modulus}}`` and ``{{load_magnitude}}`` in the INP file. The following is a snippet of the template file:

.. code-block::

    *Material, name=Material
    *Density
    2.7e-09,
    *Elastic
    {{youngs_modulus}}, 0.3

    *Step, name=Elastic, nlgeom=NO
    *Static
    1., 1., 1e-05, 1.
    **
    ** BOUNDARY CONDITIONS
    **
    *Boundary
    _PickedSet5, ENCASTRE
    *Boundary
    _PickedSet8, XSYMM
    **
    ** LOADS
    **
    ** Name: Load   Type: Pressure
    *Dsload
    _PickedSurf4, P, -{{load_magnitude}}
    **
    ** OUTPUT REQUESTS
    **
    *Restart, write, frequency=0
    *Output, field, variable=PRESELECT
    *Output, history, variable=PRESELECT
    *End Step