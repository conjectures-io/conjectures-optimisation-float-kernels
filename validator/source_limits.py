"""Fixed storage/execution ceiling, separate from the API's configurable upload policy.

The central API defaults to accepting 1 MiB. Workers must also handle previously
accepted sources when that policy is lowered, so this ceiling is not configurable.
Changing it requires a schema migration and coordinated worker deployment.
"""

MAX_SOURCE_BYTES = 4 * 1024 * 1024
