# Sphinx configuration file for the jug_hpc documentation.
#
# Build with: uv run --with-requirements docs/requirements.txt sphinx-build -W docs docs/_build/html

from jug_hpc.jug_hpc_version import __version__ as jug_hpc_version

extensions = [
    'myst_parser',
]

source_suffix = ['.rst', '.md']
master_doc = 'index'
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']
myst_heading_anchors = 3
myst_enable_extensions = ['deflist']

project = 'jug_hpc'
copyright = '2026, Luis Pedro Coelho'

# The short X.Y version.
version = '.'.join(jug_hpc_version.split('.')[:2])
# The full version, including alpha/beta/rc tags.
release = jug_hpc_version

today_fmt = '%B %d, %Y'
pygments_style = 'sphinx'
html_last_updated_fmt = '%b %d, %Y'
htmlhelp_basename = 'jug_hpcdoc'

latex_documents = [
    ('index', 'jug_hpc.tex', 'jug_hpc Documentation', 'Luis Pedro Coelho', 'manual'),
]
