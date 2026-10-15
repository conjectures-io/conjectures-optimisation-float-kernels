# Third-party notices

This repository is under the MIT licence in [LICENSE](LICENSE). Some of its files reproduce, adapt
or restate code from the projects below, whose licences ask that their notices travel with that
code; those notices are reproduced here in full. [REUSE.toml](REUSE.toml) records, file by file,
which notices apply, and `reuse lint` checks it.

If you copy a shipped kernel, contract or migration file into your own work, a submission
included, keep the notices that apply to it.

## Included in this repository

### FloatLib

[FloatLib](https://github.com/lean-dojo/FloatLib), MIT licence. The contract
(`lean/contract/`) restates FloatLib's IEEE 754 specification as the obligations a kernel must meet,
and the kernels (`kernels/`) and stage 2 (`lean/stage2/`) reproduce or adapt FloatLib's certified
binary32 word kernels and their helpers: `kernels/add/mirror` runs FloatLib's word addition step
for step on bare words, and the faster kernels are built from it.

```text
MIT License

Copyright (c) 2026 FloatLib

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### TorchLean

[TorchLean](https://github.com/lean-dojo/TorchLean), MIT licence. The tensor contract
(`lean/contract/ContractTensor.lean`) restates TorchLean's `reduceSumLeftSpec` and `bmmSpec`, and
the `sum`, `bmm` and full-entry kernels and `lean/stage2/` are written against those definitions;
`lean/stage2/Stage2/Link.lean` proves the restatements equal to TorchLean's own.

```text
MIT License

Copyright (c) 2026 TorchLean

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Alembic

[Alembic](https://github.com/sqlalchemy/alembic), MIT licence. `deploy/migrate/alembic/script.py.mako`
is Alembic's migration template, and `deploy/migrate/alembic/env.py` is written from Alembic's
environment template.

```text
Copyright 2009-2026 Michael Bayer.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Used but not included

These are downloaded by `setup.sh`, the Lean build, `scripts/install-bubblewrap.sh` or `pip`, and
none of their code is part of this repository. Each comes under its own licence, which applies to
anyone who downloads, builds or redistributes it, including in a container image.

| Component | Fetched by | Licence |
| --- | --- | --- |
| [Lean 4](https://github.com/leanprover/lean4) toolchain and [elan](https://github.com/leanprover/elan) | `setup.sh` | Apache-2.0 |
| [FloatLib](https://github.com/lean-dojo/FloatLib) and [TorchLean](https://github.com/lean-dojo/TorchLean) as Lake packages | `lean/lake-manifest.json` | MIT |
| [Mathlib](https://github.com/leanprover-community/mathlib4), [Batteries](https://github.com/leanprover-community/batteries), [Aesop](https://github.com/leanprover-community/aesop), [Qq](https://github.com/leanprover-community/quote4), [ProofWidgets](https://github.com/leanprover-community/ProofWidgets4), [Plausible](https://github.com/leanprover-community/plausible), [LeanSearchClient](https://github.com/leanprover-community/LeanSearchClient), [import-graph](https://github.com/leanprover-community/import-graph), [doc-gen4](https://github.com/leanprover/doc-gen4), [leansqlite](https://github.com/leanprover/leansqlite), [UnicodeBasic](https://github.com/fgdorais/lean4-unicode-basic), [BibtexQuery](https://github.com/dupuisf/BibtexQuery) | `lean/lake-manifest.json` | Apache-2.0 |
| [lean4-cli](https://github.com/leanprover/lean4-cli), [MD4Lean](https://github.com/acmepjz/md4lean) | `lean/lake-manifest.json` | MIT |
| [bubblewrap](https://github.com/containers/bubblewrap) | `scripts/install-bubblewrap.sh` | LGPL-2.0-or-later |
| bittensor, requests, python-multipart | `requirements/` | Apache-2.0 |
| alembic, bittensor-wallet, fastapi, loguru, pydantic, pydantic-settings, pytest, SQLAlchemy | `requirements/` | MIT |
| httpx | `requirements/` | BSD-3-Clause |
| psycopg | `requirements/` | LGPL-3.0-only |
