# Paste this over the first (pip magic) cell of step 3 and step 4, so they run from the Run jobs buttons / pipelines too.
# Edit NEEDED to what that notebook installed (step 3 and step 4: neo4j, sentence-transformers, openai).
# Packages are installed from plain Python, not with the pip magic command: Fabric disables that magic in runs that
# are not interactive (a pipeline, a schedule, or the Run jobs button), which fails the whole session. Interactive runs work too.
import importlib, importlib.util, site, subprocess, sys
NEEDED = {"neo4j": "neo4j", "sentence_transformers": "sentence-transformers", "openai": "openai"}          # import name -> pip name
missing = [pip for mod, pip in NEEDED.items() if importlib.util.find_spec(mod) is None]
if missing:
    print("installing:", missing)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *missing])
    site.addsitedir(site.getusersitepackages())
    importlib.invalidate_caches()
for mod in NEEDED:
    importlib.import_module(mod)
print("packages ready:", sorted(NEEDED.values()))
