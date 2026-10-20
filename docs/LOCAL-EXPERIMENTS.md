# Local experiments

Set up the host with `./setup.sh --service` and `export PYTHONPATH=.:validator`.

Check an entry without changing competition state:

```bash
.venv/bin/python -m verification my-entry --verdict results/trial.json
```

Run the complete disposable-DB workflow with
[scripts/test-exact-workflow.py](../scripts/test-exact-workflow.py).
It preserves the submitted source, plans, raw paired observations and decisions under the chosen
output directory and never calls the chain.

Raw experiment data belongs under `data/`; keep raw measurements separate from summaries and
visualizations. Earlier benchmark studies and frozen sources remain reference material.
