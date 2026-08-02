"""The local web console's HTTP layer.

Thin by rule: routes parse arguments and call the same core functions the CLI
calls. Anything that would have to be written twice belongs in the core instead.
"""
