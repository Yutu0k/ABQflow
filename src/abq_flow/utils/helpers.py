"""
Some helper functions for abaqus batch processing.

"""


def check_abqpy_installed() -> bool:
	try:
		import abqpy
		return True
	except ImportError:
		return False