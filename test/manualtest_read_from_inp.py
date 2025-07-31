#--coding: UTF-8-*-
# 
# This script can be run in following ways in the console:
# Note that since we import abaqus in the script, we have to use `cae` command in 1st and 2nd ways.
# 1. Directly run the script in Abaqus/CAE:
#    abaqus cae noGUI=test/test_read_from_inp.py
# 2. Via abqpy module:
#    abqpy cae test/test_read_from_inp.py --nogui
# 3. Directly run with Python if abqpy is installed:
#    python test/test_read_from_inp.py
# 

from abaqus import mdb
import sys

inpPath = 'C:/SJTU/Projects_Code/24_Abaqus_Pack/test/output/job_array_run_0001/job_array_run_0001.inp'
mdb.ModelFromInputFile(name='test', inputFileName=inpPath)

if 'Model-1' in mdb.models:
	del mdb.models['Model-1']

root_assembly = mdb.models['test'].rootAssembly
region = root_assembly.sets['ALL'].elements

GetMass = root_assembly.getMassProperties(regions=region)
mass = GetMass['mass']

# The following lines will be printed in 'abaqus.rpy' file
print(f"Total mass: {mass} kg")
print(mass * 1000)

# To print in the console
# Abaqus/CAE hiject the output to its own console, so we use sys.__stdout__
sys.__stdout__.write(f"Total mass: {mass} kg\n")


