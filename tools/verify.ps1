$ErrorActionPreference = 'Stop'

# The standard-library checks always run.  Ruff and coverage are optional
# development dependencies declared in pyproject.toml.
python -m compileall -q benes tests
python -m unittest discover -v

python -c "import ruff" 2>$null
if ($LASTEXITCODE -eq 0) {
    python -m ruff check benes tests
}

python -c "import coverage" 2>$null
if ($LASTEXITCODE -eq 0) {
    python -m coverage run -m unittest discover
    python -m coverage report
}
