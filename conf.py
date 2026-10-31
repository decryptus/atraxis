from pathlib import Path
import tomllib
metadata = tomllib.loads((Path(__file__).resolve().parent / "pyproject.toml").read_text())["project"]
project = "Atraxis"
version = release = metadata["version"]
extensions = ["myst_parser"]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
master_doc = "index"
language = "en"
exclude_patterns = ["docs/_build", "docs/index.rst", "**/AGENTS.md"]
html_theme = "alabaster"
myst_heading_anchors = 4
include_patterns = ["index.rst", "docs/**", "docs-core.md", "docs-runtime.md", "ROADMAP.md"]

# Keep user and contributor navigation separate.
templates_path = ['_templates']
html_context = {'contributor_index': 'docs/contributors', 'contributor_pages':
    ['docs/contributors', 'docs/contributing', 'docs/v1-delivery', 'docs/read-only-adapters',
     'docs/prometheus-acceptance', 'docs/source-correspondence', 'docs/certlord-correspondence',
     'docs/observation-capacity',
     'docs/release-candidate', 'docs/releases/1.0.0rc1', 'docs-core', 'ROADMAP']}
html_sidebars = {'**': ['about.html', 'documentation-tracks.html', 'localtoc.html', 'searchbox.html']}
