# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

import os
import sys
import furo
import toml
from pathlib import Path

sys.path.insert(0, os.path.abspath('../..'))
print(os.getcwd())

# --- 从 pyproject.toml 读取项目元数据 ---
pyproject_path = Path(__file__).parents[1] / "pyproject.toml"
with pyproject_path.open("r", encoding="utf-8") as f:
	pyproject_data = toml.load(f)

project_metadata = pyproject_data["project"]

project = project_metadata["name"]
author = ", ".join(author["name"] for author in project_metadata["authors"])
copyright = f'2025, {author}'
release = project_metadata["version"]

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
	'sphinx.ext.autodoc',
	'sphinx.ext.viewcode',
	'sphinx.ext.napoleon',
	'myst_parser',
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

language = 'zh-cn'

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'furo'
html_static_path = ['_static']
